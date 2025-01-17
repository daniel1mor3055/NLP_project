import torch
import torch.nn as nn

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def build_mlp(input_size, output_size, n_layers, size, config):
    """
    Args:
        input_size: int, the dimension of inputs to be given to the network
        output_size: int, the dimension of the output
        n_layers: int, the number of hidden layers of the network
        size: int, the size of each hidden layer
        config: object
    Returns:
        An instance of (a subclass of) nn.Module representing the network.
    """
    layers = [nn.Linear(input_size, size)]
    if config.policy_instance_norm:
        layers.append(nn.InstanceNorm1d(size))
    layers.append(nn.ReLU())

    for _ in range(n_layers - 1):
        input_size, size = size, size // 2
        layers.append(nn.Linear(input_size, size))
        if config.policy_instance_norm:
            layers.append(nn.InstanceNorm1d(size))
        layers.append(nn.ReLU())

    layers.append(nn.Linear(size, output_size))
    return nn.Sequential(*layers)


def np2torch(x, cast_double_to_float=True):
    """
    Utility function that accepts a numpy array and does the following:
        1. Convert to torch tensor
        2. Move it to the GPU (if CUDA is available)
        3. Optionally casts float64 to float32 (torch is picky about types)
    """
    x = torch.from_numpy(x).float().to(device)
    if cast_double_to_float and x.dtype is torch.float64:
        x = x.float()
    return x
