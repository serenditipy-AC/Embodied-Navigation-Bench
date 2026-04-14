import argparse
import json
import math
import pickle
import time
from pathlib import Path

import airsim
import cv2
import numpy as np


POS_STEP_M = 0.8
ANGLE_STEP_DEG = 8.0


def load_dataset(dataset_path: Path):
    with dataset_path.open("rb") as f:
        data = pickle.load(f)
    if not isinstance(data, list) or not data:
        raise ValueError(f"Unexpected dataset content in {dataset_path}")
    return data


def parse_args():
    parser = argparse.ArgumentParser(
        description="Replay EmbodiedNav-Bench GT trajectories in AirSim and export VO-ready images + poses."
    )
    parser.add_argument(
        "--dataset",
        default="C:/Users/vipuser/wzl/dataset/navi_data.pkl",
        help="Path to navi_data.pkl",
    )
    parser.add_argument(
        "--output-root",
        default="C:/Users/vipuser/wzl/test",
        help="Output root for sequences_jpg/ and poses/",
    )
    parser.add_argument(
        "--sample-indices",
        type=int,
        nargs="+",
        default=[0],
        help="Dataset indices to export.",
    )
    parser.add_argument(
        "--pos-step-m",
        type=float,
        default=POS_STEP_M,
        help="Maximum translation delta per interpolation step in meters.",
    )
    parser.add_argument(
        "--angle-step-deg",
        type=float,
        default=ANGLE_STEP_DEG,
        help="Maximum yaw / camera angle delta per interpolation step in degrees.",
    )
    parser.add_argument(
        "--frame-settle-sec",
        type=float,
        default=0.03,
        help="Sleep after pose update before grabbing image.",
    )
    parser.add_argument(
        "--case-prefix",
        default="case",
        help="Output case name prefix.",
    )
    parser.add_argument(
        "--camera-name",
        default="0",
        help="AirSim camera name to capture.",
    )
    parser.add_argument(
        "--image-quality",
        type=int,
        default=95,
        help="JPEG quality [0, 100].",
    )
    parser.add_argument(
        "--video-fps",
        type=float,
        default=15.0,
        help="Export video frame rate.",
    )
    return parser.parse_args()


def ensure_output_dirs(output_root: Path):
    seq_root = output_root / "sequences_jpg"
    pose_root = output_root / "poses"
    video_root = output_root / "videos"
    seq_root.mkdir(parents=True, exist_ok=True)
    pose_root.mkdir(parents=True, exist_ok=True)
    video_root.mkdir(parents=True, exist_ok=True)
    return seq_root, pose_root, video_root


def wrap_angle_rad(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


def shortest_angle_diff_rad(start: float, end: float) -> float:
    return wrap_angle_rad(end - start)


def interpolate_angle_rad(start: float, end: float, alpha: float) -> float:
    return wrap_angle_rad(start + shortest_angle_diff_rad(start, end) * alpha)



def compute_raw_yaws(points: np.ndarray, fallback_yaw: float) -> np.ndarray:
    points = np.asarray(points, dtype=np.float64)
    if len(points) == 0:
        return np.asarray([], dtype=np.float64)

    yaws = np.full(len(points), float(fallback_yaw), dtype=np.float64)
    for idx in range(len(points) - 1):
        delta = points[idx + 1] - points[idx]
        xy_norm = float(np.linalg.norm(delta[:2]))
        if xy_norm > 1e-8:
            yaws[idx] = math.atan2(delta[1], delta[0])
        elif idx > 0:
            yaws[idx] = yaws[idx - 1]
    if len(points) >= 2:
        yaws[-1] = yaws[-2]
    return yaws



def build_camera_angles(sample: dict, count: int) -> np.ndarray:
    start_ang = float(sample.get("start_ang", 0.0))
    return np.full(count, start_ang, dtype=np.float64)



def build_interpolated_states(
    start_pos: np.ndarray,
    start_yaw: float,
    start_camera_deg: float,
    raw_points: np.ndarray,
    raw_yaws: np.ndarray,
    raw_camera_angles: np.ndarray,
    pos_step_m: float,
    angle_step_deg: float,
):
    states = [(np.asarray(start_pos, dtype=np.float64), float(start_yaw), float(start_camera_deg))]

    targets = list(zip(raw_points, raw_yaws, raw_camera_angles))
    if not targets:
        return states

    prev_pos, prev_yaw, prev_cam = states[0]
    for next_pos, next_yaw, next_cam in targets:
        distance = float(np.linalg.norm(next_pos - prev_pos))
        yaw_delta_deg = abs(math.degrees(shortest_angle_diff_rad(prev_yaw, float(next_yaw))))
        cam_delta_deg = abs(float(next_cam) - float(prev_cam))

        steps = int(
            max(
                math.ceil(distance / pos_step_m) if pos_step_m > 0 else 0,
                math.ceil(yaw_delta_deg / angle_step_deg) if angle_step_deg > 0 else 0,
                math.ceil(cam_delta_deg / angle_step_deg) if angle_step_deg > 0 else 0,
            )
        )

        if steps <= 0:
            prev_pos = np.asarray(next_pos, dtype=np.float64)
            prev_yaw = float(next_yaw)
            prev_cam = float(next_cam)
            continue

        for step_idx in range(1, steps + 1):
            alpha = step_idx / steps
            pos = prev_pos + (next_pos - prev_pos) * alpha
            yaw = interpolate_angle_rad(prev_yaw, float(next_yaw), alpha)
            cam = float(prev_cam) + (float(next_cam) - float(prev_cam)) * alpha
            states.append((pos.astype(np.float64), float(yaw), float(cam)))

        prev_pos = np.asarray(next_pos, dtype=np.float64)
        prev_yaw = float(next_yaw)
        prev_cam = float(next_cam)

    return states



def euler_to_rotation_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)

    rot_x = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]], dtype=np.float64)
    rot_y = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]], dtype=np.float64)
    rot_z = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]], dtype=np.float64)
    return rot_z @ rot_y @ rot_x



def pose_to_kitti_line(position: np.ndarray, roll: float, pitch: float, yaw: float) -> str:
    rotation = euler_to_rotation_matrix(roll, pitch, yaw)
    transform = np.concatenate([rotation, position.reshape(3, 1)], axis=1)
    flat = transform.reshape(-1)
    return " ".join(f"{value:.8f}" for value in flat)



def set_vehicle_pose(client, position: np.ndarray, roll: float, pitch: float, yaw: float):
    pose = airsim.Pose(
        airsim.Vector3r(float(position[0]), float(position[1]), float(position[2])),
        airsim.to_quaternion(float(pitch), float(roll), float(yaw)),
    )
    client.simSetVehiclePose(pose, True)



def set_camera_angle(client, camera_name: str, angle_deg: float):
    camera_pose = airsim.Pose(
        airsim.Vector3r(0.0, 0.0, 0.0),
        airsim.to_quaternion(math.radians(float(angle_deg)), 0.0, 0.0),
    )
    client.simSetCameraPose(camera_name, camera_pose)



def capture_rgb_bgr(client, camera_name: str) -> np.ndarray:
    response = client.simGetImages(
        [airsim.ImageRequest(camera_name, airsim.ImageType.Scene, False, False)]
    )[0]
    if response.width <= 0 or response.height <= 0 or not response.image_data_uint8:
        raise RuntimeError("AirSim returned an empty image.")

    img1d = np.frombuffer(response.image_data_uint8, dtype=np.uint8)
    expected_size = response.width * response.height * 3
    if img1d.size != expected_size:
        raise RuntimeError(
            f"Unexpected image buffer size: got {img1d.size}, expected {expected_size}"
        )

    return img1d.reshape(response.height, response.width, 3)



def save_bgr_image(image_bgr: np.ndarray, image_path: Path, image_quality: int):
    ok = cv2.imwrite(str(image_path), image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(image_quality)])
    if not ok:
        raise RuntimeError(f"Failed to write image to {image_path}")



def open_video_writer(video_path: Path, frame_shape, fps: float):
    height, width = frame_shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(video_path), fourcc, float(fps), (int(width), int(height)))
    if not writer.isOpened():
        raise RuntimeError(f"Failed to open video writer for {video_path}")
    return writer



def export_case(
    client,
    sample: dict,
    dataset_index: int,
    case_name: str,
    seq_root: Path,
    pose_root: Path,
    video_root: Path,
    camera_name: str,
    pos_step_m: float,
    angle_step_deg: float,
    frame_settle_sec: float,
    image_quality: int,
    video_fps: float,
):
    raw_points = np.asarray(sample["gt_traj"], dtype=np.float64)
    start_pos = np.asarray(sample["start_pos"], dtype=np.float64)
    start_rot = np.asarray(sample["start_rot"], dtype=np.float64)
    start_ang = float(sample.get("start_ang", 0.0))
    start_yaw = float(start_rot[2]) if start_rot.shape[0] >= 3 else 0.0

    raw_yaws = compute_raw_yaws(raw_points, fallback_yaw=start_yaw)
    raw_camera_angles = build_camera_angles(sample, len(raw_points))
    states = build_interpolated_states(
        start_pos=start_pos,
        start_yaw=start_yaw,
        start_camera_deg=start_ang,
        raw_points=raw_points,
        raw_yaws=raw_yaws,
        raw_camera_angles=raw_camera_angles,
        pos_step_m=pos_step_m,
        angle_step_deg=angle_step_deg,
    )

    case_image_dir = seq_root / case_name / "image_2"
    case_image_dir.mkdir(parents=True, exist_ok=True)
    pose_path = pose_root / f"{case_name}.txt"
    video_path = video_root / f"{case_name}.mp4"

    init_roll = float(start_rot[0]) if start_rot.shape[0] >= 1 else 0.0
    init_pitch = float(start_rot[1]) if start_rot.shape[0] >= 2 else 0.0
    set_vehicle_pose(client, start_pos, init_roll, init_pitch, start_yaw)
    set_camera_angle(client, camera_name, start_ang)
    time.sleep(max(frame_settle_sec, 0.1))

    pose_lines = []
    video_writer = None
    try:
        for frame_idx, (position, yaw, camera_deg) in enumerate(states):
            set_vehicle_pose(client, position, 0.0, 0.0, float(yaw))
            set_camera_angle(client, camera_name, float(camera_deg))
            time.sleep(frame_settle_sec)

            image_bgr = capture_rgb_bgr(client, camera_name)
            if video_writer is None:
                video_writer = open_video_writer(video_path, image_bgr.shape, fps=video_fps)

            image_path = case_image_dir / f"{frame_idx:06d}.jpg"
            save_bgr_image(image_bgr, image_path, image_quality=image_quality)
            video_writer.write(image_bgr)
            pose_lines.append(pose_to_kitti_line(position, 0.0, 0.0, float(yaw)))
    finally:
        if video_writer is not None:
            video_writer.release()

    pose_path.write_text("\n".join(pose_lines) + "\n", encoding="utf-8")

    return {
        "case_name": case_name,
        "dataset_index": dataset_index,
        "dataset_folder": sample.get("folder"),
        "task_desc": sample.get("task_desc"),
        "start_pos": start_pos.tolist(),
        "start_rot": start_rot.tolist(),
        "start_ang": start_ang,
        "raw_traj_points": int(len(raw_points)),
        "exported_frames": int(len(states)),
        "pos_step_m": float(pos_step_m),
        "angle_step_deg": float(angle_step_deg),
        "video_fps": float(video_fps),
        "pose_file": str(pose_path),
        "image_dir": str(case_image_dir),
        "video_file": str(video_path),
    }



def main():
    args = parse_args()
    dataset_path = Path(args.dataset)
    output_root = Path(args.output_root)
    data = load_dataset(dataset_path)
    seq_root, pose_root, video_root = ensure_output_dirs(output_root)

    invalid_indices = [idx for idx in args.sample_indices if idx < 0 or idx >= len(data)]
    if invalid_indices:
        raise IndexError(f"Invalid sample indices: {invalid_indices}; dataset size is {len(data)}")

    client = airsim.VehicleClient()
    client.confirmConnection()

    manifest = []
    started = time.time()
    for dataset_index in args.sample_indices:
        case_name = f"{args.case_prefix}_{dataset_index:04d}"
        print(f"[export] {case_name} <- dataset[{dataset_index}]")
        info = export_case(
            client=client,
            sample=data[dataset_index],
            dataset_index=dataset_index,
            case_name=case_name,
            seq_root=seq_root,
            pose_root=pose_root,
            video_root=video_root,
            camera_name=args.camera_name,
            pos_step_m=args.pos_step_m,
            angle_step_deg=args.angle_step_deg,
            frame_settle_sec=args.frame_settle_sec,
            image_quality=args.image_quality,
            video_fps=args.video_fps,
        )
        manifest.append(info)
        print(
            f"[done] {case_name}: raw_points={info['raw_traj_points']}, exported_frames={info['exported_frames']}, video={info['video_file']}"
        )

    summary = {
        "dataset_path": str(dataset_path),
        "output_root": str(output_root),
        "sample_indices": args.sample_indices,
        "pos_step_m": args.pos_step_m,
        "angle_step_deg": args.angle_step_deg,
        "frame_settle_sec": args.frame_settle_sec,
        "video_fps": args.video_fps,
        "elapsed_sec": round(time.time() - started, 3),
        "cases": manifest,
    }
    manifest_path = output_root / "cases_manifest.json"
    manifest_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[summary] wrote manifest to {manifest_path}")


if __name__ == "__main__":
    main()
