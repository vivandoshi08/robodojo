"""Smoke test: confirm MuJoCo loads a model and steps physics correctly."""

import mujoco
import numpy as np

FALLING_BOX_XML = """
<mujoco>
  <option timestep="0.002" gravity="0 0 -9.81"/>
  <worldbody>
    <geom name="floor" type="plane" size="5 5 0.1"/>
    <body name="box" pos="0 0 1">
      <freejoint/>
      <geom type="box" size="0.1 0.1 0.1" mass="1"/>
    </body>
  </worldbody>
</mujoco>
"""


def test_mujoco_version():
    assert mujoco.__version__


def test_box_falls_and_rests_on_floor():
    model = mujoco.MjModel.from_xml_string(FALLING_BOX_XML)
    data = mujoco.MjData(model)

    start_z = data.qpos[2]
    assert np.isclose(start_z, 1.0)

    # Simulate 2 seconds.
    for _ in range(int(2.0 / model.opt.timestep)):
        mujoco.mj_step(model, data)

    # Box should have fallen and come to rest on the floor (half-height 0.1).
    assert data.qpos[2] < start_z
    assert np.isclose(data.qpos[2], 0.1, atol=0.01)
    assert np.linalg.norm(data.qvel) < 1e-2
