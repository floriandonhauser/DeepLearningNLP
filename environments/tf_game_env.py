"""TF python environment class for game based on TextWorld"""

from abc import ABC

import gym
import numpy as np
import textworld
import textworld.gym
from tf_agents.environments import py_environment
from tf_agents.specs import array_spec
from tf_agents.trajectories import time_step as ts

STR_TYPE = "S500"
HASH_LIST_LENGTH = 10
# all positive int!
REWARD_DICT = {
    "win_lose_value": 100,
    "max_loop_pun": 5,
    "change_reward": 1,
    "useless_act_pun": 1,
    "verb_in_adm": 1,
}


class TWGameEnv(py_environment.PyEnvironment, ABC):
    """Game environment for TextWorld games in TensorFlow agents.

    Parameters:
    -----------
    game_path: str
        Path to game file
    path_verb, path_obj: str
        Path to verb and object files to create commands as VERB + OBJ
    path_badact: str
        Path to list of bad environment observation returns from nonsense commands.
    debug: True
        Turning on/off printing of states, commands, etc.
    flatten_actspec: False
        Flattening action space from 2D (ver, obj) to list of all possible combinations
        for 1D action space.
    """

    def __init__(
        self,
        game_path: str,
        path_verb: str,
        path_obj: str,
        path_badact: str,
        debug: bool = False,
        flatten_actspec: bool = False,
    ):
        self._game_path = game_path
        self._path_verb = path_verb
        self._path_obj = path_obj
        self._path_badact = path_badact
        self._debug = debug
        self._flatten_actspec = flatten_actspec

        self._list_verb = self._get_words(self._path_verb)
        self._list_obj = self._get_words(self._path_obj)
        self._list_badact = self._get_words(self._path_badact)

        if self._flatten_actspec:
            # TODO: First obj is EMPTY and should not be printed
            self._list_verbobj = [
                v + " " + o for v in self._list_verb for o in self._list_obj
            ]
            self._action_spec = array_spec.BoundedArraySpec(
                shape=(),
                dtype=np.uint16,
                minimum=0,
                maximum=(len(self._list_verbobj) - 1),
                name="action",
            )

        else:
            self._list_verbobj = None
            self._action_spec = array_spec.BoundedArraySpec(
                shape=(2,),
                dtype=np.uint16,
                minimum=[0, 0],
                maximum=[len(self._list_verb) - 1, len(self._list_obj) - 1],
                name="action",
            )

        self._observation_spec = array_spec.ArraySpec(
            shape=(2,), dtype=STR_TYPE, name="observation"
        )

        self._hash_dsc = [0] * HASH_LIST_LENGTH
        self._hash_inv = [0] * HASH_LIST_LENGTH

        self.curr_TWGym = None
        self._state = None
        self._episode_ended = False

    def action_spec(self):
        return self._action_spec

    def observation_spec(self):
        return self._observation_spec

    def _reset(self):

        self._episode_ended = False
        self._start_game()

        return ts.restart(self._conv_pass_state(self._state))

    def _step(self, action):
        if self._episode_ended:
            # Last action ended episode. Ignore current action and start new episode.
            return self.reset()

        if self._state["done"] or self._state["won"] or self._state["lost"]:
            self._episode_ended = True

        old_state = self._state
        cmd = self._conv_to_cmd(action)
        self._state = self._conv_to_state(*self.curr_TWGym.step(cmd))
        new_state = self._state
        self._update_hash_cache(new_state)

        if self._debug:
            print(self._state)

        # TODO: adjust discount in tf_agents.trajectories.time_step.transition?
        pass_state = self._conv_pass_state(self._state)
        reward = self._calc_reward(new_state, old_state, cmd)
        if self._debug:
            print(f"Reward = {reward}")

        if self._episode_ended:
            return ts.termination(pass_state, reward)
        else:
            return ts.transition(pass_state, reward=reward, discount=1.0)

    def _start_game(self):
        """Initializing new game environment in TextWorld"""

        if self._debug:
            print("Starting new game.")

        request_info = textworld.EnvInfos(
            description=True,
            admissible_commands=True,
            entities=True,
            inventory=True,
            won=True,
            lost=True,
        )
        env_id = textworld.gym.register_game(self._game_path, request_info)
        self.curr_TWGym = gym.make(env_id)
        self.curr_TWGym.reset()

        self._state = self._conv_to_state(*self.curr_TWGym.step("look"))
        if self._debug:
            print(self._state)

    def _conv_to_cmd(self, action_ind: list):
        """Convert indices from agent into string command via imported files."""

        if self._flatten_actspec:
            cmd_str = self._list_verbobj[action_ind]
        else:
            verb = self._list_verb[action_ind[0]]
            # EMTPY obj should be empty string
            if action_ind[1] == 0:
                obj = ""
            else:
                obj = self._list_obj[action_ind[1]]
            cmd_str = verb + " " + obj
        if self._debug:
            print(f"Doing: {cmd_str}")

        return cmd_str

    def _calc_reward(self, new_state, old_state, cmd):
        """Calculate reward based on different environment returns and changes."""

        reward = 0

        # Use score difference as base reward
        reward += new_state["score"] - old_state["score"]

        # Punish useless actions from know game return statements
        if np.array([elem in new_state["obs"] for elem in self._list_badact]).sum():
            reward -= REWARD_DICT["useless_act_pun"]

        # Use change in environment description to reward changes
        inv_change = self._calc_cache_changes(self._hash_inv)
        des_change = self._calc_cache_changes(self._hash_dsc)
        if inv_change <= 1 or des_change <= 1:
            reward += REWARD_DICT["change_reward"]
        else:
            # at least 1, at max REWARD_DICT["max_loop_pun"]
            reward -= min([inv_change - 1, des_change - 1, REWARD_DICT["max_loop_pun"]])

        # Greatly reward/punish win/lose of game
        if new_state["won"]:
            reward += REWARD_DICT["win_lose_value"]
        elif new_state["lost"]:
            reward -= REWARD_DICT["win_lose_value"]

        # Check if verb in command was in admissible commands
        cmd_in_adm = self._find_verb_in_list(
            verb_str=cmd[: cmd.find(" ")], adm_cmd=new_state["admissible_commands"]
        )
        if cmd_in_adm:
            reward += REWARD_DICT["verb_in_adm"]

        return reward

    def _update_hash_cache(self, curr_state):
        """Use new state to add current desc and inv to hashed list of last states."""

        # Advanced hashing with import hashlib
        self._hash_dsc.append(hash(curr_state["description"]))
        self._hash_dsc.pop(0)
        self._hash_inv.append(hash(curr_state["inventory"]))
        self._hash_inv.pop(0)

    @staticmethod
    def _find_verb_in_list(verb_str: str, adm_cmd: list) -> bool:
        """Find whether a substring is in a list of longer strings"""

        count = np.asarray([verb_str in adm for adm in adm_cmd]).sum()
        if count >= 1:
            return True
        else:
            return False

    @staticmethod
    def _calc_cache_changes(cache: list) -> int:
        """Sum over how many times latest state was in cache"""
        return (np.asarray(cache) == cache[-1]).sum()

    @staticmethod
    def _conv_to_state(obs: str, score: int, done: bool, info: dict) -> np.array:
        """Convert TextWorld gym env output into nested array"""

        # TODO: Pre-processing text?
        return {
            "score": score,
            "done": done,
            "won": info["won"],
            "lost": info["lost"],
            "obs": obs,
            "description": info["description"],
            "inventory": info["inventory"],
            "admissible_commands": info["admissible_commands"],
            "entities": info["entities"],
        }

    @staticmethod
    def _conv_pass_state(state):
        """Select information to pass from current state and create app. np.array."""
        return np.array([state["description"], state["inventory"]], dtype=STR_TYPE)

    @staticmethod
    def _get_words(path: str):
        """Import words (verbs or objects) from verb txt file"""

        with open(path, "r") as f:
            content = [item.strip() for item in f]
        return content
