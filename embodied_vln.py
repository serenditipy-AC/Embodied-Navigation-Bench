import copy
import os
import time
import base64
import pickle
from collections import deque

import airsim
import cv2
import numpy as np
import pandas as pd
from openai import AzureOpenAI

from airsim_utils.coord_transformation import quaternion2eularian_angles


def parse_llm_action(llm_output: str) -> int:
    """
    Parse one action command from the LLM output text.

    Expected output format is usually:
    "Thinking: ...\nCommand: <action_name>"

    Returns:
        int: action enum used by `perform_act`. Returns -1 if parsing fails.
    """
    command_str = llm_output.split(":")[-1]
    command_str = command_str.strip(" ")
    command_str = command_str.lower()

    if "forth" in command_str:
        return 6
    elif "back" in command_str:
        return 7
    elif "turn_left" in command_str:
        return 2
    elif "turn_right" in command_str:
        return 3
    elif "angle_up" in command_str:
        return 4
    elif "angle_down" in command_str:
        return 5
    elif "left" in command_str:
        return 8
    elif "right" in command_str:
        return 9
    elif "up" in command_str:
        return 10
    elif "down" in command_str:
        return 11
    else:
        return -1


class ActionGen:
    """
    Agent logic for one-step action generation.

    The class keeps short conversation history and sends the current
    first-person image + textual task context to the LLM at each step.
    """

    def __init__(self, model, client, airsim_client, task_desc):
        """
        Args:
            model: model/deployment name used by the LLM endpoint.
            client: initialized LLM client.
            airsim_client: AirSim wrapper with control/perception methods.
            task_desc: text description of the navigation target.
        """
        self.model = model
        self.model_class = model.split("-")[0]
        self.llm_client = client
        self.queue = deque()
        self.messages = []  # Conversation history forwarded to the model.
        self.airsim_client = airsim_client
        self.task_desc = task_desc

    def query(self, camera_angle):
        """
        Run one decision step and return raw LLM output text.

        Args:
            camera_angle: current gimbal angle in degrees.

        Returns:
            str: LLM output string that should contain "Command: ...".
        """
        # Capture front camera RGB observation.
        img1 = self.airsim_client.get_image()

        # Encode image to base64 so it can be attached to multimodal API input.
        _, buffer = cv2.imencode(".jpg", img1)
        base64_image1 = base64.b64encode(buffer).decode("utf-8")

        # Use a longer system-style instruction for the first round only.
        if len(self.messages) == 0:
            user_content = (
                f"Please follow the instructions provided to control the camera gimbal angle and drone to gradually "
                f"move to the customer's designated location. Assuming the angle range of the camera gimbal is -90 "
                f"degrees to 90 degrees, where -90 degrees represents vertical downward view, 0 degrees represents "
                f"horizontal view, and 90 degrees represents vertical upward view.\n"
                f"\n"
                f"Camera angle commands:\n"
                f"angle_down, angle_up\n"
                f"\n"
                f"Drone movement commands:\n"
                f"move_forth, move_back, move_left, move_right, move_up, move_down, turn_left, turn_right\n"
                f"\n"
                f"Example:\n"
                f"The navigation goal is: main entrance of the building directly below. "
                f"The current angle of the camera gimbal is {camera_angle}.\n"
                f"Thinking: Should first lower the altitude and then search.\n"
                f"Command: move_forth\n"
                f"\n"
                f"Rule: put reasoning after 'Thinking'. After 'Command:', output only one executable command with no "
                f"extra text.\n"
                f"\n"
                f"The navigation goal is: {self.task_desc}. "
                f"The current angle of the camera gimbal is {camera_angle}.\n"
                f"Note: avoid spinning in place repeatedly.\n"
                f"\n"
                f"Thinking:\n"
                f"Command:"
            )
        else:
            user_content = (
                f"The navigation goal is: {self.task_desc}. "
                f"The current angle of the camera gimbal is {camera_angle}.\n"
                f"Continue to output the thinking and command to approach the destination.\n"
                f"Thinking:\n"
                f"Command:"
            )

        # Call the OpenAI-compatible chat completion API.
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_content},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{copy.deepcopy(base64_image1)}"
                        },
                    },
                ],
            }
        )

        try:
            chat_response = self.llm_client.chat.completions.create(
                model=self.model,
                messages=self.messages,
            )
            answer = chat_response.choices[0].message.content
            print(f"GPT: {answer}")
        except Exception as e:
            print(f"Error: LM response - {e}")
            answer = "Error"

        self.messages.append({"role": "assistant", "content": answer})
        return answer


class AirsimClient:
    """Minimal AirSim wrapper for this benchmark script."""

    def __init__(self, vehicle_name=""):
        _ = vehicle_name  # Reserved for future multi-vehicle extension.
        airsim_client = airsim.VehicleClient()
        airsim_client.confirmConnection()
        self.client = airsim_client

    def set_vehicle_pose(self, position, orientation):
        """
        Teleport the vehicle to the target pose.

        Args:
            position: xyz array in world coordinates.
            orientation: roll/pitch/yaw array in radians.
        """
        client = self.client
        pose = airsim.Pose(airsim.Vector3r(*position), airsim.to_quaternion(*orientation))
        client.simSetVehiclePose(pose, True)

    def set_camera_angle(self, angle):
        """
        Set camera gimbal pitch angle (degrees).
        """
        client = self.client
        camera_pose = airsim.Pose(
            airsim.Vector3r(0, 0, 0),
            airsim.to_quaternion(angle * np.pi / 180, 0, 0),
        )
        client.simSetCameraPose("0", camera_pose)

    def move_relative(self, dx, dy, dz):
        """
        Move relative to the drone local coordinate system.

        Args:
            dx: forward/backward displacement.
            dy: right/left displacement.
            dz: up/down displacement.
        """
        client = self.client
        pose = client.simGetVehiclePose()
        orientation = airsim.to_eularian_angles(pose.orientation)
        yaw = orientation[2]

        # Convert local displacement into world-frame displacement.
        forward = np.array([np.cos(yaw), np.sin(yaw), 0])
        right = np.array([-np.sin(yaw), np.cos(yaw), 0])
        up = np.array([0, 0, 1])
        move_vector = dx * forward + dy * right + dz * up
        new_position = np.array(
            [pose.position.x_val, pose.position.y_val, pose.position.z_val]
        ) + move_vector

        self.set_vehicle_pose(new_position, orientation)

    def get_current_state(self):
        """
        Get current pose from AirSim.

        Returns:
            tuple[np.ndarray, np.ndarray]: position and euler orientation.
        """
        client = self.client
        state = client.simGetGroundTruthKinematics()
        pos = state.position.to_numpy_array()
        ori = quaternion2eularian_angles(state.orientation)
        return pos, ori

    def get_image(self):
        """
        Get RGB observation from the front camera.
        """
        response = self.client.simGetImages(
            [airsim.ImageRequest("0", airsim.ImageType.Scene, False, False)]
        )
        img1d = np.frombuffer(response[0].image_data_uint8, dtype=np.uint8)
        if img1d.size == (response[0].height * response[0].width * 3):
            img_rgb = img1d.reshape(response[0].height, response[0].width, 3)
            return img_rgb
        return None


class VLN_evaluator:
    """
    Evaluation pipeline for vision-language navigation.
    """

    def __init__(self, root_dir, eval_model, llm_client, agent_method):
        """
        Args:
            root_dir: dataset root directory.
            eval_model: model/deployment name.
            llm_client: initialized LLM client.
            agent_method: label used in output result directory.
        """
        self.root_dir = root_dir
        self.eval_model = eval_model
        self.airsim_client = AirsimClient()
        self.agent_method = agent_method
        self.llm_client = llm_client
        self.load_navi_task()

    def load_navi_task(self):
        """Load navigation tasks from `navi_data.pkl`."""
        with open(os.path.join(self.root_dir, "navi_data.pkl"), "rb") as f:
            self.navi_data = pickle.load(f)

    def evaluation(self):
        """
        Evaluate navigation performance and print SR/SPL/DTG.
        """
        navi_data = self.navi_data
        navi_data_pd = pd.DataFrame(navi_data)

        # Split samples into short/middle/long groups by trajectory length quantiles.
        short_len = navi_data_pd["gt_traj_len"].quantile(1 / 3)
        middle_len = navi_data_pd["gt_traj_len"].quantile(2 / 3)
        sr_count_sets = np.zeros((3,))
        num_sets = np.zeros((3,))
        ne_count_sets = np.zeros((3,))
        spl_sets = np.zeros((3,))

        # Aggregate metrics over all samples.
        sr_count = 0.0
        spl = 0.0
        ne_count = 0.0

        # Evaluate each navigation sample independently.
        for idx in range(len(navi_data)):
            navi_task = navi_data[idx]
            start_pos = navi_task["start_pos"]
            start_rot = navi_task["start_rot"]
            gt_traj = navi_task["gt_traj"]
            target_pos = navi_task["target_pos"]
            gt_traj_len = navi_task["gt_traj_len"]
            task_desc = navi_task["task_desc"]
            _ = gt_traj  # Reserved for future path-level metrics.

            # Initialize agent for this sample.
            agent = ActionGen(self.eval_model, self.llm_client, self.airsim_client, task_desc)

            # Reset drone pose and camera angle.
            self.airsim_client.set_vehicle_pose(start_pos, start_rot)
            self.camera_angle = 0
            self.airsim_client.set_camera_angle(self.camera_angle)
            print(f"Current navigation goal: {task_desc}")

            # Print current state.
            cur_pos, cur_rot = self.airsim_client.get_current_state()
            print(f"pos: {cur_pos}, rot: {cur_rot}")

            # Log full executed trajectory for this sample.
            traj_df = pd.DataFrame(columns=["pos", "rot", "camera_angle"])
            traj_df.loc[traj_df.shape[0]] = [start_pos, start_rot, self.camera_angle]

            traj_len = 0.0
            step = 0
            max_steps = 50
            threshold = 20

            # Step-by-step control loop.
            while step < max_steps:
                # Query one action from the agent.
                answer = agent.query(self.camera_angle)

                # Parse command text into an internal action enum.
                act = parse_llm_action(answer)
                print("action: ", act)

                # Execute action in simulator.
                self.perform_act(act)
                time.sleep(0.1)

                former_pos = cur_pos
                cur_pos, cur_rot = self.airsim_client.get_current_state()
                traj_df.loc[traj_df.shape[0]] = [cur_pos, cur_rot, self.camera_angle]
                traj_len += np.linalg.norm(cur_pos - former_pos)
                step += 1

                # Distance to goal after this step.
                dist = np.linalg.norm(cur_pos - target_pos)
                print(f"Task idx: {idx}, current step size: {step}, current dist: {dist}")

                # Stop on success or if the drone has diverged too far.
                if dist < threshold:
                    break
                elif dist > 300:
                    break

            # Final distance for this sample.
            print(f"Max step size reached or target reached. step: {step}")
            dist = np.linalg.norm(cur_pos - target_pos)

            # Save predicted trajectory.
            save_folder_path = "results/%s/%s" % (self.agent_method, self.eval_model)
            if not os.path.exists(save_folder_path):
                os.makedirs(save_folder_path)
            traj_df.to_csv(os.path.join(save_folder_path, "%d.csv" % idx), index=False)

            # Update group-level DTG accumulators.
            if gt_traj_len < short_len:
                num_sets[0] += 1
                ne_count_sets[0] += dist
            elif gt_traj_len < middle_len:
                num_sets[1] += 1
                ne_count_sets[1] += dist
            else:
                num_sets[2] += 1
                ne_count_sets[2] += dist

            # Update SR/SPL if success.
            if dist < threshold:
                sr_count += 1
                spl_count = gt_traj_len / max(gt_traj_len, traj_len)
                spl += spl_count

                if gt_traj_len < short_len:
                    sr_count_sets[0] += 1
                    spl_sets[0] += gt_traj_len / max(gt_traj_len, traj_len)
                elif gt_traj_len < middle_len:
                    sr_count_sets[1] += 1
                    spl_sets[1] += gt_traj_len / max(gt_traj_len, traj_len)
                else:
                    sr_count_sets[2] += 1
                    spl_sets[2] += gt_traj_len / max(gt_traj_len, traj_len)

            ne_count += dist
            print(f"####### SR count: {sr_count}, SPL: {spl}, NE: {ne_count}")
            print("Group SR:", sr_count_sets / num_sets)
            print("Group SPL:", spl_sets / num_sets)
            print("Group DTG:", ne_count_sets / num_sets)
            print("Group sample counts:", num_sets)

        # Final overall metrics.
        sr = sr_count / len(navi_data)
        ne = ne_count / len(navi_data)
        print(f"SR: {sr}, SPL: {spl}, NE: {ne}")
        np.set_printoptions(precision=3)
        print("Group SR:", sr_count_sets / num_sets)
        print("Group SPL:", spl_sets / num_sets)
        print("Group DTG:", ne_count_sets / num_sets)

    def perform_act(self, act_enum):
        """
        Execute one parsed action enum in AirSim.
        """
        # Action table: enum -> (name, value)
        # - tuple value: relative translation (dx, dy, dz)
        # - scalar value: rotation in degrees or camera angle delta in degrees
        commands_map = {
            6: ("move_forth", (10, 0, 0)),
            7: ("move_back", (-10, 0, 0)),
            8: ("move_left", (0, -10, 0)),
            9: ("move_right", (0, 10, 0)),
            10: ("move_up", (0, 0, -10)),
            11: ("move_down", (0, 0, 10)),
            2: ("turn_left", -22.5),
            3: ("turn_right", 22.5),
            4: ("angle_up", 45),
            5: ("angle_down", -45),
        }

        try:
            command, value = commands_map[act_enum]

            if command in ["angle_up", "angle_down"]:
                # Clamp gimbal angle to the valid range [-90, 90].
                self.camera_angle += value
                self.camera_angle = max(-90, min(90, self.camera_angle))
                self.airsim_client.set_camera_angle(self.camera_angle)
            elif act_enum in commands_map.keys():
                # Movement or yaw rotation.
                if isinstance(value, tuple):
                    dx, dy, dz = value
                    self.airsim_client.move_relative(dx, dy, dz)
                else:
                    yaw_change = value
                    pose = self.airsim_client.client.simGetVehiclePose()
                    current_orientation = airsim.to_eularian_angles(pose.orientation)
                    new_orientation = [
                        current_orientation[0],
                        current_orientation[1],
                        current_orientation[2] + np.radians(yaw_change),
                    ]
                    self.airsim_client.set_vehicle_pose(
                        [pose.position.x_val, pose.position.y_val, pose.position.z_val],
                        new_orientation,
                    )
            else:
                print(f"Unknown action {act_enum}, keep still.")
        except Exception:
            pass


if __name__ == "__main__":
    # Configure your model deployment and credentials before running this file.
    #
    # Recommended setup:
    # 1) Fill values via environment variables:
    #    AZURE_OPENAI_MODEL
    #    AZURE_OPENAI_API_KEY
    #    AZURE_OPENAI_ENDPOINT
    #    AZURE_OPENAI_API_VERSION (optional, defaults to 2024-07-01-preview)
    #
    # 2) Or directly replace the placeholder strings below.
    model = os.getenv("AZURE_OPENAI_MODEL", "YOUR_AZURE_OPENAI_DEPLOYMENT")
    api_key = os.getenv("AZURE_OPENAI_API_KEY", "YOUR_AZURE_OPENAI_API_KEY")
    azure_endpoint = os.getenv(
        "AZURE_OPENAI_ENDPOINT",
        "https://YOUR-RESOURCE-NAME.openai.azure.com/",
    )
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-07-01-preview")

    if (
        model == "YOUR_AZURE_OPENAI_DEPLOYMENT"
        or api_key == "YOUR_AZURE_OPENAI_API_KEY"
        or azure_endpoint == "https://YOUR-RESOURCE-NAME.openai.azure.com/"
    ):
        raise ValueError(
            "Azure OpenAI is not configured.\n"
            "Set environment variables (AZURE_OPENAI_MODEL, AZURE_OPENAI_API_KEY, "
            "AZURE_OPENAI_ENDPOINT, optional AZURE_OPENAI_API_VERSION) or replace "
            "the placeholder values in `embodied_vln.py` before running."
        )

    llm_client = AzureOpenAI(
        api_key=api_key,
        api_version=api_version,
        azure_endpoint=azure_endpoint,
    )

    # Name used in output directory: results/<agent_method>/<model>/
    agent_method = "action_generation"

    # Initialize evaluator and run all tasks in dataset/navi_data.pkl.
    vln_eval = VLN_evaluator("dataset", model, llm_client, agent_method)
    vln_eval.evaluation()
