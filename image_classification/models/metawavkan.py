'''This is a sample code for the simulations of the paper:
Bozorgasl, Zavareh and Chen, Hao, Wav-KAN: Wavelet Kolmogorov-Arnold Networks (May, 2024)

https://arxiv.org/abs/2405.12832
and also available at:
https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4835325
We used efficient KAN notation and some part of the code:https://github.com/Blealtan/efficient-kan

'''
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
from torch.utils.data import DataLoader
from tqdm import tqdm
import math
from torch.nn.utils.parametrizations import spectral_norm


def linear_layer(input_dim, output_dim):
    linear = nn.Linear(input_dim, output_dim)
    nn.init.xavier_normal_(linear.weight)
    nn.init.constant_(linear.bias, 0)
    return linear

class HyperNetwork(nn.Module):
    def __init__(self, input_dim, hidden_dim, output_dim):
        super(HyperNetwork, self).__init__()
        self.layers = nn.Sequential(
            linear_layer(input_dim, hidden_dim),
            nn.ReLU(),
            linear_layer(hidden_dim, output_dim)
        )
    def forward(self, x):
        return self.layers(x)

class KANLinear(nn.Module):
    def __init__(self, in_features, out_features, wavelet_type='mexican_hat'):
        super(KANLinear, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.wavelet_type = wavelet_type

        # Parameters for wavelet transformation
        self.scale = torch.ones(out_features, in_features)
        self.translation = torch.zeros(out_features, in_features)
    
        # Linear weights for combining outputs
        #self.weight = nn.Parameter(torch.Tensor(out_features, in_features))
        self.weight1 = nn.Parameter(torch.Tensor(out_features, in_features)) #not used; you may like to use it for wieghting base activation and adding it like Spl-KAN paper
        self.wavelet_weights = torch.Tensor(out_features, in_features)

        nn.init.kaiming_uniform_(self.wavelet_weights, a=math.sqrt(5))
        nn.init.kaiming_uniform_(self.weight1, a=math.sqrt(5))

        # Base activation function #not used for this experiment
        self.base_activation = nn.SiLU()

        # Batch normalization
        self.bn = nn.BatchNorm1d(out_features)

    def wavelet_transform(self, x):
        if x.dim() == 2:
            x_expanded = x.unsqueeze(1)
        else:
            x_expanded = x
        
        translation_expanded = self.translation.unsqueeze(0).expand(x.size(0), -1, -1)
        scale_expanded = self.scale.unsqueeze(0).expand(x.size(0), -1, -1)

        x_scaled = (x_expanded - translation_expanded) / scale_expanded

        # Implementation of different wavelet types
        if self.wavelet_type == 'mexican_hat':
            term1 = ((x_scaled ** 2)-1)
            term2 = torch.exp(-0.5 * x_scaled ** 2)
            wavelet = (2 / (math.sqrt(3) * math.pi**0.25)) * term1 * term2
            wavelet_weighted = wavelet * self.wavelet_weights.unsqueeze(0).expand_as(wavelet)
            wavelet_output = wavelet_weighted.sum(dim=2)
        elif self.wavelet_type == 'morlet':
            omega0 = 5.0  # Central frequency
            real = torch.cos(omega0 * x_scaled)
            envelope = torch.exp(-0.5 * x_scaled ** 2)
            wavelet = envelope * real
            wavelet_weighted = wavelet * self.wavelet_weights.unsqueeze(0).expand_as(wavelet)
            wavelet_output = wavelet_weighted.sum(dim=2)
            
        elif self.wavelet_type == 'dog':
            # Implementing Derivative of Gaussian Wavelet 
            dog = -x_scaled * torch.exp(-0.5 * x_scaled ** 2)
            wavelet = dog
            wavelet_weighted = wavelet * self.wavelet_weights.unsqueeze(0).expand_as(wavelet)
            wavelet_output = wavelet_weighted.sum(dim=2)
        elif self.wavelet_type == 'meyer':
            # Implement Meyer Wavelet here
            # Constants for the Meyer wavelet transition boundaries
            v = torch.abs(x_scaled)
            pi = math.pi

            def meyer_aux(v):
                return torch.where(v <= 1/2,torch.ones_like(v),torch.where(v >= 1,torch.zeros_like(v),torch.cos(pi / 2 * nu(2 * v - 1))))

            def nu(t):
                return t**4 * (35 - 84*t + 70*t**2 - 20*t**3)
            # Meyer wavelet calculation using the auxiliary function
            wavelet = torch.sin(pi * v) * meyer_aux(v)
            wavelet_weighted = wavelet * self.wavelet_weights.unsqueeze(0).expand_as(wavelet)
            wavelet_output = wavelet_weighted.sum(dim=2)
        elif self.wavelet_type == 'shannon':
            # Windowing the sinc function to limit its support
            pi = math.pi
            sinc = torch.sinc(x_scaled / pi)  # sinc(x) = sin(pi*x) / (pi*x)

            # Applying a Hamming window to limit the infinite support of the sinc function
            window = torch.hamming_window(x_scaled.size(-1), periodic=False, dtype=x_scaled.dtype, device=x_scaled.device)
            # Shannon wavelet is the product of the sinc function and the window
            wavelet = sinc * window
            wavelet_weighted = wavelet * self.wavelet_weights.unsqueeze(0).expand_as(wavelet)
            wavelet_output = wavelet_weighted.sum(dim=2)
            #You can try many more wavelet types ...
        else:
            raise ValueError("Unsupported wavelet type")

        return wavelet_output

    def forward(self, x, weights):
        self.wavelet_weights = weights[:,:,2]
        self.translation = weights[:,:,1]
        self.scale = weights[:,:,0]
        wavelet_output = self.wavelet_transform(x)
        #You may like test the cases like Spl-KAN
        #wav_output = F.linear(wavelet_output, self.weight)
        #base_output = F.linear(self.base_activation(x), self.weight1)

        base_output = F.linear(x, self.weight1)
        combined_output =  wavelet_output #+ base_output 

        # Apply batch normalization
        return self.bn(combined_output)

class HyperWavKAN(nn.Module):
    def __init__(self, args):
        super(HyperWavKAN, self).__init__()
        self.layers = nn.ModuleList()
        self.embedding_dim = args.embedding_dim
        self.hypernet = HyperNetwork(args.embedding_dim, args.hidden_dim, 3)
        self.embeddings = nn.ParameterList()

        layers_hidden = [args.input_size] + args.layers_width + [args.output_size]
        for in_features, out_features in zip(layers_hidden[:-1], layers_hidden[1:]):
            embedding = nn.Parameter(torch.randn(in_features, out_features, args.embedding_dim))
            nn.init.xavier_normal_(embedding)
            self.embeddings.append(embedding)            
            self.layers.append(KANLinear(in_features, out_features, args.wavelet_type))

    def forward(self, x):
        for l, layer in enumerate(self.layers):
            weights = self.hypernet(self.embeddings[l].view(-1,self.embedding_dim)).view(layer.out_features, layer.in_features, 3)
            x = layer(x, weights)
        return x