import contextlib
import logging
import os
from datetime import datetime

import numpy as np
import torch
import wandb

from policy_search.baseline_network import BaselineNetwork
from utils.network_utils import build_mlp, device, np2torch
from policy_search.policy import CategoricalPolicy
from policy_search.episode import Episode
from torchsummary import summary

from utils.utils import CaptureStdout

logger = logging.getLogger('root')


class PolicyGradient(object):
    """
    Class for implementing a policy gradient algorithm
    """

    def __init__(self, env, config):
        # directory for training outputs

        if not os.path.exists(config.output_path):
            os.makedirs(config.output_path)

        # store hyperparameters
        self.config = config

        self.env = env

        # state and action space
        self.observation_dim, self.action_dim = self.env.observation_space, self.env.action_space
        self.lr = self.config.learning_rate

        self.init_policy()

        if config.baseline:
            self.baseline_network = BaselineNetwork(self.env, config)

        with CaptureStdout() as capture:
            summary(self.policy.network, input_size=(self.observation_dim,))

        logger.info(f"Policy initialized with:"
                    f"\n{capture.get_output()}"
                    f"{self.optimizer=})"
                    f"\n{self.observation_dim=}, "
                    f"{self.action_dim=}, "
                    f"{self.lr=}")

    def init_policy(self):
        self._network = build_mlp(
            input_size=self.observation_dim,
            output_size=self.action_dim,
            n_layers=self.config.n_layers,
            size=self.config.first_layer_size,
            config=self.config
        )

        self.policy = CategoricalPolicy(self._network, self.config).to(device)

        self.optimizer = torch.optim.Adam(self.policy.parameters(), lr=self.lr)

    def sample_episode(self, current_batch):
        observation = self.env.reset()
        episode = Episode()
        done = False

        while not done:
            action, _ = self.policy.act(observation.reshape(1, -1), current_batch)
            next_observation, reward, done, _ = self.env.step(action.item())
            episode.add(observation, action.item(), reward)
            observation = next_observation

        return episode

    def sample_episodes(self, current_batch):
        episodes = []

        for i in range(self.config.num_episodes_per_batch):
            episode = self.sample_episode(current_batch)
            episodes.append(episode)

        return episodes

    def get_returns(self, episodes):
        """
        Calculate the discounted cumulative returns G_t for each timestep in the provided episodes.

        Args:
            episodes (list): A list of episodes. Each episode is expected to have a 'rewards' attribute
                             which is a np.array of the corresponding rewards for each timestep in the episode.

        Returns:
            np.array: A np.array containing the discounted cumulative returns G_t for each timestep
                          across all episodes. The array shape is (total_timesteps), where
                          total_timesteps is the sum of the number of timesteps across all episodes.
        """

        all_returns = []
        for episode in episodes:
            rewards = episode.rewards
            returns = np.zeros_like(rewards, dtype=np.float64)

            G_t = 0
            for t in reversed(range(len(rewards))):
                G_t = rewards[t] + self.config.gamma * G_t
                returns[t] = G_t
            all_returns.append(returns)

        # Stack all the returns into a single tensor
        returns = np.concatenate(all_returns)
        return returns

    def normalize_advantage(self, advantages):
        """
        Args:
            advantages: np.array of shape [batch size]
        Returns:
            normalized_advantages: np.array of shape [batch size]
        """
        mean_advantage = np.mean(advantages)
        std_advantage = np.std(advantages)
        # Adding a small epsilon to avoid division by zero
        normalized_advantages = (advantages - mean_advantage) / (std_advantage + 1e-8)
        return normalized_advantages

    def calculate_advantage(self, returns, observations):
        """
        Calculates the advantage for each of the observations
        Args:
            returns: np.array of shape [batch size]
            observations: np.array of shape [batch size, dim(observation space)]
        Returns:
            advantages: np.array of shape [batch size]
        """
        if self.config.baseline:
            # override the behavior of advantage by subtracting baseline
            advantages = self.baseline_network.calculate_advantage(
                returns, observations
            )
        else:
            advantages = returns  # baseline is 0 in case of no baseline

        if self.config.normalize_advantage:
            advantages = self.normalize_advantage(advantages)

        return advantages

    def update_policy(self, observations, actions, advantages, current_batch):
        """
        Args:
            observations: np.array of shape [batch size, dim(observation space)]
            actions: np.array of shape
                [batch size, dim(action space)] if continuous
                [batch size] (and integer type) if discrete
            advantages: np.array of shape [batch size]
        """
        observations = np2torch(observations)
        actions = np2torch(actions)
        advantages = np2torch(advantages)

        # Get log probabilities of the actions
        action_dists = self.policy.action_distribution(observations, current_batch)
        log_probs = action_dists.log_prob(actions)

        # Zero out the gradients from the previous pass
        self.optimizer.zero_grad()

        # Compute the loss function
        loss = -(log_probs * advantages).mean()

        # Backward pass to compute gradients and update the policy
        loss.backward()
        self.optimizer.step()

    def merge_episodes_to_batch(self, episodes):
        observations = np.concatenate([episode.observations for episode in episodes])
        actions = np.concatenate([episode.actions for episode in episodes])

        logger.debug(f"chosen actions in entire batch: {actions}")

        # compute Q-val estimates (discounted future returns) for each time step
        returns = self.get_returns(episodes)

        # advantage will depend on the baseline implementation
        advantages = self.calculate_advantage(returns, observations)

        batch_rewards = np.array([episode.total_reward for episode in episodes])

        return observations, actions, returns, advantages, batch_rewards

    def _init_wandb(self):
        fields_to_exclude = ['output_path', 'model_output', 'log_path', 'scores_output', 'plot_output', 'BASE_DIR',
                             'dataset', 'seed']

        wandb.init(
            project="NLP_project",
            name=datetime.now().strftime('%Y-%m-%d_%H-%M-%S') + f'-{self.config.run_name}',
            config={k: v for k, v in vars(self.config).items() if k not in fields_to_exclude}
        )

    # TODO - save model every x timestamps
    def train(self):
        averaged_total_rewards = []
        for t in range(self.config.num_batches):
            episodes = self.sample_episodes(current_batch=t)
            observations, actions, returns, advantages, batch_rewards = self.merge_episodes_to_batch(episodes)

            # run training operations
            if self.config.baseline:
                self.baseline_network.update_baseline(returns, observations)

            self.update_policy(observations, actions, advantages, current_batch=t)

            avg_batch_reward = batch_rewards.mean()
            std_batch_reward = batch_rewards.std()
            msg = "[ITERATION {}]: Average reward: {:04.2f} +/- {:04.2f}".format(
                t + 1, avg_batch_reward, std_batch_reward
            )
            averaged_total_rewards.append(avg_batch_reward)
            logger.info(msg)

            # WANDB LOG
            if self.config.run_name:
                wandb.log({"avg_batch_reward": avg_batch_reward, "std_batch_reward": std_batch_reward})

    # TODO - fix add logic
    def evaluate(self, env=None, num_episodes_per_batch=1):
        pass
        # if env == None:
        #     env = self.env
        # paths, rewards = self.sample_paths(env, num_episodes_per_batch)
        # avg_reward = np.mean(rewards)
        # sigma_reward = np.sqrt(np.var(rewards) / len(rewards))
        # msg = "Average reward: {:04.2f} +/- {:04.2f}".format(avg_reward, sigma_reward)
        # self.logger.info(msg)
        # return avg_reward

    @contextlib.contextmanager
    def wandb_context(self):
        if self.config.run_name:
            wandb.login()
            self._init_wandb()
        yield
        if self.config.run_name:
            wandb.finish()

    def run(self):
        """
        Apply procedures of training for a PG.
        """
        logger.info("Start Training...")
        with self.wandb_context():
            try:
                self.train()
            except Exception as e:
                logger.error(f"Training finished unexpectedly - {e}")
            finally:
                logger.info("Training completed...")
