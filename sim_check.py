"""Quick check that MuJoCo runs: drop a box and print its height over time."""

import mujoco

XML = """
<mujoco>
  <worldbody>
    <geom type="plane" size="5 5 0.1"/>
    <body pos="0 0 1">
      <freejoint/>
      <geom type="box" size="0.1 0.1 0.1"/>
    </body>
  </worldbody>
</mujoco>
"""

model = mujoco.MjModel.from_xml_string(XML)
data = mujoco.MjData(model)

print(f"MuJoCo {mujoco.__version__}")
while data.time < 2.0:
    mujoco.mj_step(model, data)
    if data.time % 0.25 < model.opt.timestep:
        print(f"t={data.time:.2f}s  z={data.qpos[2]:.3f}")
