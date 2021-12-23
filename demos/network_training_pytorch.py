""" PyTorch Network Training Demo
Trains a simple CNN to make velocity fields incompressible.
This script runs for a certain number of steps before saving the trained network and halting.
"""
from phi.torch.flow import *


# TORCH.set_default_device('GPU')
net = u_net(2, 2).to(TORCH.get_default_device().ref)
optimizer = optim.Adam(net.parameters(), lr=1e-3)


@vis.action
def save_model():
    path = viewer.scene.subpath(f"net_{viewer.steps}.pth" if viewer.scene else f"net_{viewer.steps}.pth")
    torch.save(net.state_dict(), path)
    viewer.info(f"Model saved to {path}.")
    # To load the network: net.load_state_dict(torch.load('net.pth'))


prediction = CenteredGrid((0, 0), extrapolation.BOUNDARY, x=64, y=64)
prediction_div = CenteredGrid(0, 0, x=64, y=64)
viewer = view(play=False, namespace=globals(), select='batch')

for step in viewer.range(100):
    # Load or generate training data
    data = CenteredGrid(Noise(batch(batch=8), channel(vector=2)), extrapolation.BOUNDARY, x=64, y=64)
    # Initialize optimizer
    optimizer.zero_grad()
    # Prediction
    prediction = field.native_call(net, data)  # calls net with shape (BATCH_SIZE, channels, spatial...)
    # Simulation
    prediction_div = field.divergence(prediction)
    # Define loss
    loss = field.l2_loss(prediction_div) + field.l2_loss(prediction - data)
    viewer.log_scalars(loss=loss.mean, div=field.mean(abs(prediction_div)).mean, distance=math.vec_abs(field.mean(abs(prediction - data))).mean)
    # Compute gradients and update weights
    loss.mean.backward()
    optimizer.step()

save_model()
