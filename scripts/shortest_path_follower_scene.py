import math
import os
import random
import sys
import json

import cv2
import imageio
import ipdb
import numpy as np
import argparse
import tqdm

# function to display the topdown map
from PIL import Image

import habitat_sim
from habitat_sim.utils import common as utils
from habitat_sim.utils import viz_utils as vut

import habitat
from habitat.core.utils import try_cv2_import
from habitat.tasks.nav.shortest_path_follower import ShortestPathFollower
from habitat_sim.nav import GreedyGeodesicFollower
from habitat.utils.visualizations import maps
from habitat.utils.visualizations.utils import images_to_video
from habitat_sim.utils.common import quat_from_angle_axis

from scipy.spatial.transform import Rotation as R

from utils import split_data


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scene_id",
        type=str,
        default="./data/scene_datasets/habitat-test-scenes/skokloster-castle.glb",
        help="specify glb file ")
    parser.add_argument(
        "--out_dir",
        default=os.path.join("data", "shortest_path_scene"),
        help="output directory to store recorded data ",
    )
    parser.add_argument(
        "--num_episodes",
        type=int,
        default=100,
        help="Number of episodes to collect data for",
    )
    parser.add_argument(
        "--max_steps",
        type=int,
        default=128,
        help="maximum steps allowed per episode")
    parser.add_argument(
        "--min_steps",
        type=int,
        default=5,
        help="minimum steps allowed per episode")
    parser.add_argument(
        "--step_thresh",
        type=int,
        default=10,
        help="minimum trajectory length to consider an episode valid")
    parser.add_argument(
        "--linear_speed",
        type=float,
        default=0.25,
        help="linear speed / forward step size (in meters)")
    parser.add_argument(
        "--angular_speed",
        type=float,
        default=5.0,
        help="angular speed / left or right step size (in degrees) ")
    parser.add_argument(
        "--seed",
        type=int,
        default=9211,
        help="set seed for random events and reproducibility")
    parser.add_argument(
        "--split",
        type=float,
        default=0.3,
        help="Percentage of trajectories in validation set")
    args = parser.parse_args()
    return args


def simulator_settings(rgb_sensor=True, depth_sensor=False):
    sim_settings = {
        "width": 256,  # Spatial resolution of the observations
        "height": 256,
        "scene": args.scene_id,  # Scene path
        "default_agent": 0,
        "sensor_height": 0.5,  # Height of sensors in meters
        "color_sensor": rgb_sensor,  # RGB sensor
        "depth_sensor": depth_sensor,  # Depth sensor
        "seed": 1,  # used in the random navigation
        "enable_physics": False,  # kinematics only
        "linear_speed": args.linear_speed,
        "angular_speed": args.angular_speed
    }
    return sim_settings


def make_cfg(settings):
    '''
    Setting up configuration to initialize the simulator and the agent. Adding sensors, specifying action space etc. 

    Returns:
        cfg: Configuration for both agent and simulator
    '''
    sim_cfg = habitat_sim.SimulatorConfiguration()
    sim_cfg.gpu_device_id = 0
    sim_cfg.scene_id = settings["scene"]
    sim_cfg.enable_physics = settings["enable_physics"]

    # Note: all sensors must have the same resolution
    sensor_specs = []

    color_sensor_spec = habitat_sim.CameraSensorSpec()
    color_sensor_spec.uuid = "color_sensor"
    color_sensor_spec.sensor_type = habitat_sim.SensorType.COLOR
    color_sensor_spec.resolution = [settings["height"], settings["width"]]
    color_sensor_spec.position = [0.0, settings["sensor_height"], 0.0]
    color_sensor_spec.sensor_subtype = habitat_sim.SensorSubType.PINHOLE
    sensor_specs.append(color_sensor_spec)

    depth_sensor_spec = habitat_sim.CameraSensorSpec()
    depth_sensor_spec.uuid = "depth_sensor"
    depth_sensor_spec.sensor_type = habitat_sim.SensorType.DEPTH
    depth_sensor_spec.resolution = [settings["height"], settings["width"]]
    depth_sensor_spec.position = [0.0, settings["sensor_height"], 0.0]
    depth_sensor_spec.sensor_subtype = habitat_sim.SensorSubType.PINHOLE
    sensor_specs.append(depth_sensor_spec)

    # Here you can specify the amount of displacement in a forward action and the turn angle
    agent_cfg = habitat_sim.agent.AgentConfiguration()
    agent_cfg.sensor_specifications = sensor_specs
    agent_cfg.action_space = {
        "move_forward": habitat_sim.agent.ActionSpec(
            "move_forward", habitat_sim.agent.ActuationSpec(amount=settings["linear_speed"])
        ),
        "turn_left": habitat_sim.agent.ActionSpec(
            "turn_left", habitat_sim.agent.ActuationSpec(amount=settings["angular_speed"])
        ),
        "turn_right": habitat_sim.agent.ActionSpec(
            "turn_right", habitat_sim.agent.ActuationSpec(amount=settings["angular_speed"])
        ),
    }

    cfg = habitat_sim.Configuration(sim_cfg, [agent_cfg])

    return cfg


def euler_to_quaternion(yaw, pitch, roll):
    '''
        Converts Euler angles representation to Quaternion 
    '''
    qx = np.sin(roll / 2) * np.cos(pitch / 2) * np.cos(yaw / 2) - np.cos(roll / 2) * np.sin(pitch / 2) * np.sin(yaw / 2)
    qy = np.cos(roll / 2) * np.sin(pitch / 2) * np.cos(yaw / 2) + np.sin(roll / 2) * np.cos(pitch / 2) * np.sin(yaw / 2)
    qz = np.cos(roll / 2) * np.cos(pitch / 2) * np.sin(yaw / 2) - np.sin(roll / 2) * np.sin(pitch / 2) * np.cos(yaw / 2)
    qw = np.cos(roll / 2) * np.cos(pitch / 2) * np.cos(yaw / 2) + np.sin(roll / 2) * np.sin(pitch / 2) * np.sin(yaw / 2)

    # return [qw, qx, qy, qz]
    return np.quaternion(qw, qx, qy, qz)


def get_step_dict(step, best_action, agent_state):
    step_dict = {}
    position, rot = agent_state.position, agent_state.rotation
    step_dict["time"] = step;
    step_dict["actions"] = {'motion': best_action}
    step_dict["reward"] = 0;
    step_dict["done"] = 0;
    step_dict["pose"] = {"x": float(position[0]), 'y': float(position[2]), 'z': float(position[1]), \
                         "orientation.x": rot.x, "orientation.y": rot.z, "orientation.z": rot.y, "orientation.w": rot.w}
    return step_dict


def save_json(dict_, filename):
    with open(filename, "w") as f:
        json.dump(dict_, f, indent=4)


def collect_data(args, out_dir, seed=42):
    # Simulator specifications 
    sim_settings = simulator_settings()

    # setting up the simulator 
    cfg = make_cfg(sim_settings)
    sim = habitat_sim.Simulator(cfg)

    # Initializing agent and the greedy Geodesic follower policy 
    agent = sim.initialize_agent(sim_settings["default_agent"])
    # follower = ShortestPathFollower(sim, 0.25, False)
    follower = GreedyGeodesicFollower(sim.pathfinder, agent, goal_radius=0.25)

    sim.seed(seed)
    random.seed(seed)

    action_space = [None] + list(cfg.agents[0].action_space.keys())

    for episode in tqdm.tqdm(range(args.num_episodes)):
        # Reset the environment 
        obs = sim.reset()
        start_state = habitat_sim.AgentState()
        goal_state = habitat_sim.AgentState()

        # Sample Random navigable points as start and goal states 
        start_state.position = sim.pathfinder.get_random_navigable_point()
        goal_state.position = sim.pathfinder.get_random_navigable_point()

        # Randomly sample the rotations for start and goal states
        rot = quat_from_angle_axis(np.deg2rad(random.randint(-180, 180)), habitat_sim.geo.UP)
        start_state.rotation = rot
        # start_state.rotation = euler_to_quaternion(0, random.randint(0, 360) * np.pi / 180, 0)
        # goal_state.rotation = R.random().as_quat() # Not being used right now. 

        # Set the agent start state
        agent = sim.initialize_agent(sim_settings["default_agent"])
        agent.set_state(start_state)
        sim.get_agent(0).state = start_state

        # Creating directories 
        try:
            os.system("mkdir -p %s" % os.path.join(out_dir, "traj_%d" % episode, "images"))
            os.system("mkdir -p %s" % os.path.join(out_dir, "traj_%d" % episode, "meta"))
        except:
            pass

        step = 0;
        while step < args.max_steps:

            agent_state = agent.get_state()
            try:
                # https://github.com/facebookresearch/habitat-sim/issues/410
                best_action = follower.next_action_along(goal_state.position)
            except:
                # Pathfinder can fail, sample new goal position and continue
                goal_state.position = sim.pathfinder.get_random_navigable_point()
                print("Resampling new goal as follower failed")
                continue
            # If the goal was reached but min number of steps is not reached, start again
            if best_action is None:
                # If we already have min number of steps break
                if step > args.min_steps:
                    break
                else:
                    goal_state.position = sim.pathfinder.get_random_navigable_point()
                    continue

            step_dict = get_step_dict(step, action_space.index(best_action), agent_state)
            save_json(step_dict, os.path.join(out_dir, "traj_%d" % episode, "meta", "%d.json" % step))
            obs = sim.get_sensor_observations()
            img_path = os.path.join(out_dir, "traj_%d" % episode, "images", "%d.png" % step)
            os.path.join(out_dir, "traj_%d" % episode, "meta", "%d.json" % step)
            im = obs["color_sensor"][:, :, :3]
            im = cv2.cvtColor(im, cv2.COLOR_RGB2BGR)  # converting to BGR (as expected by the cv2.imwrite)
            cv2.imwrite(img_path, im)
            # Do transition here!
            obs = sim.step(best_action)

            step += 1

        # Save the last point in the trajectory as last step data and goal image
        obs = sim.get_sensor_observations()
        im = obs["color_sensor"][:, :, :3]
        im = cv2.cvtColor(im, cv2.COLOR_RGB2BGR)
        agent_state = agent.get_state()
        cv2.imwrite(os.path.join(out_dir, "traj_%d" % episode, "goal.png"), im)
        # Save the goal state 
        goal_dict = get_step_dict(0, action_space.index(best_action), agent_state)
        save_json(goal_dict, os.path.join(out_dir, "traj_%d" % episode, "goal.json"))
        # Rejects trajectories of length smaller than a given threshold 
        if step < args.step_thresh:
            os.system("rm -rf %s" % os.path.join(out_dir, "traj_%d" % episode))


def S1_fixed(args, out_dir, seed=42):
    # Simulator specifications 
    sim_settings = simulator_settings()

    # setting up the simulator 
    cfg = make_cfg(sim_settings)
    sim = habitat_sim.Simulator(cfg)

    # Initializing agent 
    agent = sim.initialize_agent(sim_settings["default_agent"])

    sim.seed(seed)
    random.seed(seed)

    action_space = [None] + list(cfg.agents[0].action_space.keys())

    start_state = habitat_sim.AgentState()
    # Sample a random navigable point as start state which will be fixed for every episode 
    start_state.position = sim.pathfinder.get_random_navigable_point()

    # Every episode starts from a random pose/orientation and ends after a random number of steps 
    # the last step will count as the goal image/pose. 
    for episode in tqdm.tqdm(range(args.num_episodes)):
        # Reset the environment 
        obs = sim.reset()
        goal_state = habitat_sim.AgentState()

        # Randomly sample the rotations for start and goal states
        rot = quat_from_angle_axis(np.deg2rad(random.randint(-180, 180)), habitat_sim.geo.UP)
        start_state.rotation = rot

        # start_state.rotation = euler_to_quaternion(0, random.randint(0, 360) * np.pi / 180, 0)
        # goal_state.rotation = R.random().as_quat() # Not being used right now. 

        # Set the agent start state
        agent = sim.initialize_agent(sim_settings["default_agent"])
        agent.set_state(start_state)
        sim.get_agent(0).state = start_state

        # Creating directories 
        try:
            os.system("mkdir -p %s" % os.path.join(out_dir, "traj_%d" % episode, "images"))
            os.system("mkdir -p %s" % os.path.join(out_dir, "traj_%d" % episode, "meta"))
        except:
            pass

        # Randomly select number of steps for the episode
        if int(360 / args.angular_speed) > args.min_steps:
            num_steps = random.randint(args.min_steps, min(int(360 / args.angular_speed), args.max_steps))
        else:
            num_steps = random.randint(args.min_steps, args.max_steps)

        # Randomly select an action b/w left and right and keep executing 
        best_action = random.choice(["turn_left", "turn_right"])

        step = 0
        while step < num_steps:
            agent_state = agent.get_state()
            step_dict = get_step_dict(step, action_space.index(best_action), agent_state)
            obs = sim.get_sensor_observations()

            save_json(step_dict, os.path.join(out_dir, "traj_%d" % episode, "meta", "%d.json" % step))
            img_path = os.path.join(out_dir, "traj_%d" % episode, "images", "%d.png" % step)
            os.path.join(out_dir, "traj_%d" % episode, "meta", "%d.json" % step)
            im = obs["color_sensor"][:, :, :3]
            im = cv2.cvtColor(im, cv2.COLOR_RGB2BGR)  # converting to BGR (as expected by the cv2.imwrite)
            cv2.imwrite(img_path, im)
            # Do the transition here! (Next image)
            obs = sim.step(best_action)

            step += 1
        # Save the last point in the trajectory as last step data and goal image
        im = obs["color_sensor"][:, :, :3]
        im = cv2.cvtColor(im, cv2.COLOR_RGB2BGR)
        agent_state = agent.get_state()
        cv2.imwrite(os.path.join(out_dir, "traj_%d" % episode, "goal.png"), im)
        # Save the goal state 
        goal_dict = get_step_dict(0, action_space.index(best_action), agent_state)
        save_json(goal_dict, os.path.join(out_dir, "traj_%d" % episode, "goal.json"))
        # Rejects trajectories of length smaller than a given threshold 
        if step < args.step_thresh:
            os.system("rm -rf %s" % os.path.join(out_dir, "traj_%d" % episode))


if __name__ == "__main__":
    args = get_args()
    random.seed(args.seed)
    np.random.seed(args.seed)
    out_dir = os.path.join(args.out_dir, args.scene_id.split("/")[-1][:-4])
    # S1_fixed(args, out_dir, seed=args.seed)  # Collect training data
    collect_data(args, out_dir, seed=args.seed)  # Collect training data
    split_data(split=args.split, path=out_dir)
