# coding=utf-8
# Copyright (c) DIRECT Contributors

import torch.nn as nn


class Conv2d(nn.Module):
    """Implementation of a simple cascade of 2D convolutions.

    If batchnorm is set to True, batch normalization layer is applied after each convolution.
    """

    def __init__(self, in_channels, out_channels, hidden_channels, n_convs=3, activation=nn.PReLU(), batchnorm=False):
        """Inits Conv2d.

        Parameters
        ----------
        in_channels: int
            Number of input channels.
        out_channels: int
            Number of output channels.
        hidden_channels: int
            Number of hidden channels.
        n_convs: int
            Number of convolutional layers.
        activation: nn.Module
            Activation function.
        batchnorm: bool
            If True a batch normalization layer is applied after every convolution.
        """
        super().__init__()

        self.conv = []
        for idx in range(n_convs):
            self.conv.append(
                nn.Conv2d(
                    in_channels if idx == 0 else hidden_channels,
                    hidden_channels if idx != n_convs - 1 else out_channels,
                    kernel_size=3,
                    padding=1,
                )
            )
            if batchnorm:
                self.conv.append(nn.BatchNorm2d(hidden_channels if idx != n_convs - 1 else out_channels, eps=1e-4))
            if idx != n_convs - 1:
                self.conv.append(activation)
        self.conv = nn.Sequential(*self.conv)

    def forward(self, x):
        """Performs the forward pass of Conv2d.

        Parameters
        ----------
        x: torch.Tensor
            Input tensor.

        Returns
        -------
        out: torch.Tensor
            Convoluted output.
        """
        out = self.conv(x)
        return out
