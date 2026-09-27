"""Builds the MuJoCo scene: Franka Panda (MuJoCo Menagerie) + table + trash bin + one trash item.

Everything the physics sees lives here. Tune grasp physics (friction, mass, sizes) in ITEMS.
"""
from __future__ import annotations

import os

import mujoco
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANDA_XML = os.path.join(ROOT, "assets", "menagerie", "franka_emika_panda", "panda.xml")

HOME_Q = np.array([0.0, 0.0, 0.0, -1.57079, 0.0, 1.57079, -0.7853])  # arm joints 1-7
TCP_OFFSET = 0.1034  # hand frame -> point between the fingertips (m)

TABLE = dict(center=(0.55, -0.15), half=(0.2, 0.25), height=0.2)
BIN = dict(center=(0.45, 0.35), half_inner=0.11, wall=0.01, height=0.22)

# Trash items. size follows MuJoCo conventions (half-sizes / radius, half-length).
ITEMS = {
    "can":    dict(type="cylinder", size=[0.03, 0.05], mass=0.06, rgba=[0.85, 0.1, 0.1, 1]),
    "bottle": dict(type="capsule", size=[0.028, 0.07], mass=0.08, rgba=[0.2, 0.55, 0.95, 1], lying=True),
    "paper":  dict(type="sphere", size=[0.034], mass=0.03, rgba=[0.95, 0.95, 0.88, 1]),
    "box":    dict(type="box", size=[0.06, 0.03, 0.03], mass=0.05, rgba=[0.9, 0.7, 0.3, 1]),
}
GEOM_TYPES = {
    "cylinder": mujoco.mjtGeom.mjGEOM_CYLINDER,
    "capsule": mujoco.mjtGeom.mjGEOM_CAPSULE,
    "sphere": mujoco.mjtGeom.mjGEOM_SPHERE,
    "box": mujoco.mjtGeom.mjGEOM_BOX,
}


def item_half_height(name: str) -> float:
    it = ITEMS[name]
    t, s = it["type"], it["size"]
    if t == "sphere" or it.get("lying"):
        return s[0]
    if t == "cylinder":
        return s[1]
    if t == "capsule":
        return s[1] + s[0]
    return s[2]  # box


def look_at(pos, target):
    """MuJoCo camera xyaxes for a camera at pos looking at target."""
    pos, target = np.asarray(pos, float), np.asarray(target, float)
    fwd = target - pos
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, [0, 0, 1])
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    return list(right) + list(up)


def build_model(item: str, item_pos, item_yaw: float, bin_center=None) -> tuple[mujoco.MjModel, dict]:
    """Returns (model, layout). layout is the geometry the task + prompt need."""
    if not os.path.exists(PANDA_XML):
        raise FileNotFoundError(f"{PANDA_XML} missing. Run: bash scripts/fetch_assets.sh")
    spec = mujoco.MjSpec.from_file(PANDA_XML)
    for k in list(spec.keys):  # keyframe no longer matches once we add objects
        spec.delete(k)
    spec.visual.global_.offwidth = 1920
    spec.visual.global_.offheight = 1080

    spec.body("hand").add_site(name="tcp", pos=[0, 0, TCP_OFFSET], size=[0.005, 0, 0], rgba=[0, 1, 0, 0.5])

    # Looks: sky gradient + checker floor.
    spec.add_texture(name="sky", type=mujoco.mjtTexture.mjTEXTURE_SKYBOX, builtin=mujoco.mjtBuiltin.mjBUILTIN_GRADIENT,
                     rgb1=[0.55, 0.7, 0.85], rgb2=[0.05, 0.05, 0.08], width=512, height=3072)
    spec.add_texture(name="grid", type=mujoco.mjtTexture.mjTEXTURE_2D, builtin=mujoco.mjtBuiltin.mjBUILTIN_CHECKER,
                     rgb1=[0.82, 0.82, 0.82], rgb2=[0.72, 0.72, 0.72], width=300, height=300)
    mat = spec.add_material(name="grid", texrepeat=[6, 6], reflectance=0.1)
    mat.textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = "grid"

    wb = spec.worldbody
    wb.add_light(pos=[0.4, 0.0, 2.5], dir=[0, 0, -1], diffuse=[0.8, 0.8, 0.8], castshadow=True)
    wb.add_light(pos=[1.5, -1.0, 1.5], dir=[-1, 0.6, -0.8], diffuse=[0.35, 0.35, 0.35], castshadow=False)
    wb.add_geom(name="floor", type=mujoco.mjtGeom.mjGEOM_PLANE, size=[3, 3, 0.1], material="grid")

    tc, th, tz = TABLE["center"], TABLE["half"], TABLE["height"]
    wb.add_geom(name="table", type=mujoco.mjtGeom.mjGEOM_BOX, pos=[tc[0], tc[1], tz / 2],
                size=[th[0], th[1], tz / 2], rgba=[0.55, 0.4, 0.26, 1])

    bc = bin_center or BIN["center"]
    hi, w, bh = BIN["half_inner"], BIN["wall"], BIN["height"]
    b = wb.add_body(name="bin", pos=[bc[0], bc[1], 0])
    green = [0.18, 0.5, 0.3, 1]
    b.add_geom(name="bin_floor", type=mujoco.mjtGeom.mjGEOM_BOX, pos=[0, 0, w], size=[hi + w, hi + w, w], rgba=green)
    for i, (dx, dy, sx, sy) in enumerate([(hi + w, 0, w, hi + 2 * w), (-(hi + w), 0, w, hi + 2 * w),
                                           (0, hi + w, hi + 2 * w, w), (0, -(hi + w), hi + 2 * w, w)]):
        b.add_geom(name=f"bin_wall{i}", type=mujoco.mjtGeom.mjGEOM_BOX, pos=[dx, dy, bh / 2], size=[sx, sy, bh / 2], rgba=green)

    it = ITEMS[item]
    quat = np.zeros(4)
    mujoco.mju_euler2Quat(quat, [0, np.pi / 2 if it.get("lying") else 0, item_yaw], "xyz")
    ib = wb.add_body(name="item", pos=list(item_pos), quat=list(quat))
    ib.add_freejoint(name="item_free")
    ib.add_geom(name="item", type=GEOM_TYPES[it["type"]], size=it["size"] + [0] * (3 - len(it["size"])),
                mass=it["mass"], rgba=it["rgba"], friction=[1.5, 0.01, 0.001], condim=4)

    wb.add_camera(name="front", pos=[1.55, -0.1, 0.95], xyaxes=look_at([1.55, -0.1, 0.95], [0.35, 0.1, 0.2]), fovy=50)
    wb.add_camera(name="side", pos=[0.5, -1.35, 0.75], xyaxes=look_at([0.5, -1.35, 0.75], [0.4, 0.1, 0.2]), fovy=50)
    wb.add_camera(name="top", pos=[0.45, 0.1, 1.7], xyaxes=[0, -1, 0, 1, 0, 0], fovy=45)

    model = spec.compile()
    layout = dict(
        table=dict(center=list(tc), half_size=list(th), height=tz),
        bin=dict(center=[bc[0], bc[1], 0.0], inner_half_size=hi, rim_height=bh),
        item=dict(name=item, shape=it["type"], size=it["size"], mass=it["mass"], lying=bool(it.get("lying"))),
    )
    return model, layout
