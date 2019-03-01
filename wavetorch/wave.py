import torch
from torch.nn.functional import conv2d
from torch import tanh

class WaveCell(torch.nn.Module):

    def __init__(self, dt, Nx, Ny, src_x, src_y, probe_x, probe_y, pml_N=20, pml_p=4.0, pml_max=3.0, c0=1.0, c1=0.9, binarized=True):
        super(WaveCell, self).__init__()

        self.dt = dt

        self.Nx = Nx
        self.Ny = Ny
        self.h  = dt * 2.01 / 1.0

        self.src_x = src_x
        self.src_y = src_y
        self.probe_x = probe_x
        self.probe_y = probe_y

        self.binarized = binarized
        self.c0 = c0
        self.c1 = c1

        # Setup the PML/adiabatic absorber
        self.register_buffer("b", self.init_b(Nx, Ny, pml_N, pml_p, pml_max))

        # if binarized, rho represents the density
        # if NOT binarized, rho directly represents c
        # bounds on c are not enforced for unbinarized
        if binarized:
            rho = self.init_rho_rand(Nx, Ny)
        else:
            rho = torch.ones(Nx, Ny) * c0

        self.rho = torch.nn.Parameter(rho)
        self.clip_pml_rho()

        # Define the laplacian conv kernel
        self.register_buffer("laplacian", self.h**(-2) * torch.tensor([[[[0.0,  1.0, 0.0], [1.0, -4.0, 1.0], [0.0,  1.0, 0.0]]]]))

        # Define the finite differencing coeffs (for convenience)
        self.register_buffer("A1", (self.dt**(-2) + self.b * 0.5 * self.dt**(-1)).pow(-1))
        self.register_buffer("A2", torch.tensor(2 * self.dt**(-2)))
        self.register_buffer("A3", self.dt**(-2) - self.b * 0.5 * self.dt**(-1))

    def clip_pml_rho(self):
        with torch.no_grad():
            if self.binarized:
                self.rho[self.b!=0] = 0.0
            else:
                self.rho[self.b!=0] = self.c0

    @staticmethod
    def proj(x, eta=torch.tensor(0.5), beta=torch.tensor(100.0)):
        return (tanh(beta*eta) + tanh(beta*(x-eta))) / (tanh(beta*eta) + tanh(beta*(1-eta)))

    def c(self):
        if self.binarized:
            # apply LPF
            lpf_rho = conv2d(self.rho.unsqueeze(0).unsqueeze(0), torch.tensor([[[[0, 1/8, 0], [1/8, 1/2, 1/8], [0, 1/8, 0]]]]), padding=1).squeeze()
            #apply projection
            return (self.c0 + (self.c1-self.c0)*self.proj(lpf_rho))
        else:
            return self.rho

    @staticmethod
    def init_b(Nx, Ny, pml_N, pml_p, pml_max):
        b_vals = pml_max * torch.linspace(0.0, 1.0, pml_N+1) ** pml_p

        b_x = torch.zeros(Nx, Ny)
        b_x[0:pml_N+1,   :] = torch.flip(b_vals, [0]).repeat(Ny,1).transpose(0, 1)
        b_x[(Nx-pml_N-1):Nx, :] = b_vals.repeat(Ny,1).transpose(0, 1)

        b_y = torch.zeros(Nx, Ny)
        b_y[:,   0:pml_N+1] = torch.flip(b_vals, [0]).repeat(Nx,1)
        b_y[:, (Ny-pml_N-1):Ny] = b_vals.repeat(Nx,1)

        return torch.sqrt( b_x**2 + b_y**2 )

    @staticmethod
    def init_rho_rand(Nx, Ny, Nconv=10):
        rho = torch.rand(Nx, Ny)
        # Low pass filter a number of times to make "islands" in c
        for i in range(Nconv):
            rho = conv2d(rho.unsqueeze(0).unsqueeze(0), torch.tensor([[[[0, 1/8, 0], [1/8, 1/2, 1/8], [0, 1/8, 0]]]]), padding=1).squeeze()
        return rho

    def step(self, x, y1, y2, c):
        # Using torc.mul() lets us easily broadcast over batches
        y = torch.mul( self.A1, ( torch.mul(self.A2, y1) 
                                   - torch.mul(self.A3, y2) 
                                   + torch.mul( c.pow(2), conv2d(y1.unsqueeze(1), self.laplacian, padding=1).squeeze(1) ) ))
        
        # Insert the source
        y[:, self.src_x, self.src_y] = y[:, self.src_x, self.src_y] + x.squeeze(1)
        
        return y, y, y1

    def forward(self, x, probe_output=True):
        # hacky way of figuring out if we're on the GPU from inside the model
        device = "cuda" if next(self.parameters()).is_cuda else "cpu"
        
        # First dim is batch
        batch_size = x.shape[0]
        
        # init hidden states
        y1 = torch.zeros(batch_size, self.Nx, self.Ny, device=device)
        y2 = torch.zeros(batch_size, self.Nx, self.Ny, device=device)
        y_all = []

        # loop through time
        c = self.c()
        for i, xi in enumerate(x.chunk(x.size(1), dim=1)):
            y, y1, y2 = self.step(xi, y1, y2, c)
            y_all.append(y)

        # combine into output field dist 
        y = torch.stack(y_all, dim=1)

        if probe_output:
            # Return only the one-hot output
            return self.integrate_probe_points(self.probe_x, self.probe_y, y)
        else:
            # Return the full field distribution in time
            return y

    @staticmethod
    def integrate_probe_points(probe_x, probe_y, y):
        I = torch.sum(torch.abs(y[:, :, probe_x, probe_y]).pow(2), dim=1)
        return I / torch.sum(I, dim=1, keepdim=True)


