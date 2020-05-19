import os
import time
import subprocess

from pathlib import Path
from ruamel.yaml import YAML

from builder.modules.valid import Validator
from builder.modules.load import StateLoader
from builder.modules.target import Target
from builder.color_print import *

yaml = YAML()

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

builder_files = list(Path(root_dir).glob('app.py')) + \
                list(Path(root_dir).glob('builder/*.yml'))

valid_files = list(Path(root_dir).glob('hub/hub/**/*.y*ml')) + \
              list(Path(root_dir).glob('hub/hub/**/*Dockerfile')) + \
              list(Path(root_dir).glob('hub/hub/**/*.py'))
ignore_files = list(Path(root_dir).glob('hub/hub/**/jina/**/*')) + \
               list(Path(root_dir).glob('hub/hub/.github/**/*')) + \
               list(Path(root_dir).glob('hub/hub/builder/**/*'))
hub_files = list(set(valid_files) - set(ignore_files))


class Builder:

    def __init__(self, args):
        self.args = args

    def run(self):
        state = StateLoader()
        history = state.get_history()
        if self.args.target:
            target = Target(self.args.target)
            self.build_single(target, history)
        else:
            get_all = False
            if self.args.update_strategy == 'on-release':
                get_all = True
            targets = self.get_targets(history, get_all)
            if self.args.check_targets:
                if len(targets) == 0:
                    print(print_green('Nothing to build'))
                    exit(1)
                else:
                    exit(0)
            for path in targets:
                target = Target(path)
                if self.check_update_strategy(target):
                    if self.args.bleach_first:
                        self.clean_docker()
                    self.build_single(target, history)
        state.update_total_history(history)

    @staticmethod
    def clean_docker():
        print(print_green('Removing all existing docker instances'))
        for k in ['df -h',
                  'docker stop $(docker ps -aq)',
                  'docker rm $(docker ps -aq)',
                  'docker rmi -f $(docker image ls -aq)',
                  'df -h']:
            try:
                subprocess.check_call(k, shell=True)
            except subprocess.CalledProcessError:
                pass

    def build_single(self, target, history):
        output = None
        status = 'fail'
        try:
            validator = Validator(target)
            try:
                output = target.build_image(push=self.args.push, test=self.args.test)
                status = 'success'
            except Exception as e:
                print(print_red(e) + f' while building {target.canonic_name}')
        except Exception as e:
            print(print_red(e) + f' while validating {target.canonic_name}')

        image_map = {
            'Status': bool(output),
            'LastBuildTime': int(time.time()),
            'Inspect': output
        }
        history['Images'][target.canonic_name] = image_map
        history['LastBuildTime'][target.canonic_name] = int(time.time())
        history['LastBuildStatus'][target.canonic_name] = status
        history['LastBuildReason'] = self.args.reason or self.args.update_strategy or 'test'

    def get_targets(self, history, get_all):
        to_be_updated_targets = set()
        builder_updated_timestamp = self.get_builder_update_history()

        for file_path in hub_files:
            target = os.path.dirname(os.path.abspath(file_path))
            canonic_name = os.path.relpath(target).replace('/', '.').strip('.')

            modified_time = self.get_modified_time(file_path)
            last_build_timestamp = history.get('LastBuildTime', {}).get(canonic_name, 0)
            is_target_to_be_added = False
            if builder_updated_timestamp > last_build_timestamp or modified_time > last_build_timestamp:
                is_target_to_be_added = True

            if is_target_to_be_added or get_all:
                to_be_updated_targets.add(target)
                print(print_green('Found target file ') + str(file_path))
                print(f'Last build time: {self.get_hr_time(last_build_timestamp) if last_build_timestamp else None}')
                print(f'Target modified time: {self.get_hr_time(modified_time) if modified_time else None}')

        return to_be_updated_targets

    @staticmethod
    def get_modified_time(file_path) -> int:
        r = subprocess.check_output(['git', 'log', '-1', '--pretty=%at', str(file_path)]).strip().decode()
        if r:
            return int(r)
        else:
            print(print_red(f'\nCan\'t fetch modified time of {file_path}, is it under git?'))
            return 0

    @staticmethod
    def get_hr_time(stamp):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))

    def get_builder_update_history(self):
        last_builder_update_timestamp = 0
        for file_path in builder_files:
            update_timestamp = self.get_modified_time(file_path)
            if update_timestamp > last_builder_update_timestamp:
                last_builder_update_timestamp = update_timestamp
        print(print_green('Last builder update: ') + self.get_hr_time(last_builder_update_timestamp))
        return last_builder_update_timestamp

    @staticmethod
    def get_canonic_name(target):
        return os.path.relpath(target).replace('/', '.').strip('.')

    def check_update_strategy(self, target):

        event_map = {
            'force': 0,
            'manually': 20,
            'on-release': 30,
            'nightly': 40,
            'on-master': 50
        }

        strategy_map = {
            'never': 10,
            'manually': 20,
            'on-release': 30,
            'nightly': 40,
            'on-master': 50
        }

        target_strategy = target.manifest.get('update', 'nightly')
        current_strategy = self.args.update_strategy
        try:
            event_level = event_map[current_strategy]
            update_level = strategy_map[target_strategy]
            if event_level >= update_level:
                return True
        except KeyError as e:
            print(print_red(f'{e} is not valid strategy for ') + target.canonic_name)
