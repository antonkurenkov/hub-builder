import os
import json
from pymongo import MongoClient
import time
from pathlib import Path
import subprocess
import unicodedata

from ruamel.yaml import YAML
yaml = YAML()

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
build_hist_path = os.path.join(root_dir, 'status', 'build-history.json')

builder_files = list(Path(root_dir).glob('app.py')) + \
                list(Path(root_dir).glob('builder/*.yml'))

valid_files = list(Path(root_dir).glob('hub/**/*.y*ml')) + \
            list(Path(root_dir).glob('hub/**/*Dockerfile')) + \
            list(Path(root_dir).glob('hub/**/*.py'))
ignore_files = list(Path(root_dir).glob('hub/**/jina/**/*'))
hub_files = list(set(valid_files) - set(ignore_files))


class Mongo:

    def __init__(self):
        credentials = os.getenv('MONGODB_CREDENTIALS')
        if credentials:
            address = f"mongodb+srv://{credentials}@cluster0-irout.mongodb.net/test?retryWrites=true&w=majority"
            client = MongoClient(address)
            self.db = client['jina-test']
        else:
            print(f'Incorrect credentials "{credentials}" for DB connection.')
            exit(1)

    def create_or_update(self, **kwargs):
        spec = {'_id': 1}
        self.db.docker.replace_one(filter=spec, replacement=dict(**kwargs), upsert=True)

    def get_from_database(self):
        spec = {'_id': 1}
        return self.db.docker.find_one(filter=spec)

    # def select_head(self, n):
    #     return dict(self.db.docker.find().limit(n))
    #
    # def contains(self, item):  #  -> int | bool
    #     return dict(self.db.docker.find(item).count())


class Loader(Mongo):

    def __init__(self, args):
        super().__init__()
        self.target: str = args.target
        self.error_on_empty: str = args.error_on_empty

        self.dockerfile_path = None
        self.manifest_path = None
        self.manifest = None

        self.last_build_time = {}
        self.image_map = {}
        self.status_map = {}

        self.last_builder_update_timestamp = 0
        self.img_name = ''

    @staticmethod
    def get_from_local():
        if os.path.isfile(build_hist_path):
            with open(build_hist_path, 'r') as fp:
                hist = json.load(fp)
            return hist

    def get_targets(self):
        to_be_updated_targets = set()
        is_builder_updated = False

        for file_path in hub_files:
            modified_time = self.get_modified_time(file_path)
            target = os.path.dirname(os.path.abspath(file_path))
            canonic_name = self.get_canonic_name(target)
            last_image_build_time = self.last_build_time.get(canonic_name, 0)

            is_target_to_be_added = False
            if self.last_builder_update_timestamp > last_image_build_time:
                is_builder_updated = True
                is_target_to_be_added = True
            elif modified_time > last_image_build_time:
                is_target_to_be_added = True

            if is_target_to_be_added:
                to_be_updated_targets.add(target)
                print(print_green('\nFound target file ') + str(file_path))
                print(f'Last build time: {last_image_build_time or "---"}')
                print(f'Target modified time: {self.get_hr_time(modified_time)}')

        return to_be_updated_targets, is_builder_updated

    def load_build_history(self):
        db_hist = self.get_from_database()
        local_hist = self.get_from_local()
        hist = db_hist or local_hist
        if hist is None:
            print(print_red('\nCan\'t load build history from database or ') + build_hist_path)
            hist = {}
        else:
            self.last_build_time = hist.get('LastBuildTime', {})
            print(print_green('\nLast build time:'))
            print(
                *[f'[{name}] -> {self.get_hr_time(timestamp)}' for name, timestamp in self.last_build_time.items()],
                sep='\n'
            )

        self.image_map = hist.get('Images', {})
        self.status_map = hist.get('LastBuildStatus', {})

    def load_builder_update_history(self):
        for file_path in builder_files:
            updated_timestamp = self.get_modified_time(file_path)
            if updated_timestamp > self.last_builder_update_timestamp:
                self.last_builder_update_timestamp = updated_timestamp
        print(print_green('Last builder update: ') + self.get_hr_time(self.last_builder_update_timestamp))

    def construct_paths(self):
        if os.path.isdir(self.target):
            self.dockerfile_path = os.path.join(self.target, 'Dockerfile')
            self.manifest_path = os.path.join(self.target, 'manifest.yml')
            return True
        elif self.error_on_empty:
            raise NotADirectoryError(f'{os.path.join(os.getcwd(), self.target)} is not a valid directory')

    def load_manifest(self):
        with open(self.manifest_path) as yml:
            self.manifest = yaml.load(yml)

    def load_fields_from_manifest(self):
        print(print_green('\nManifest file ') + self.manifest_path)
        for key, value in self.manifest.items():
            updated_value = self.remove_control_characters(value)
            if updated_value != value:
                self.manifest[key] = updated_value
                print(print_purple(f'{key}: {value} -> {updated_value}') + ' // removed invalid chars')
            else:
                print(print_purple(f'{key}: {value}'))
        self.add_platform_revision_source()

    @staticmethod
    def remove_control_characters(source):
        return ''.join(ch for ch in source if not unicodedata.category(ch).startswith('C'))

    def add_platform_revision_source(self):
        self.manifest['platform'] = self.manifest.get('platform') or []
        print(print_purple(f"platform: {self.manifest['platform'] or '---'}"))

        self.manifest['revision'] = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).strip().decode()
        print(print_purple(f"revision: {self.manifest['revision']}"))

        self.manifest['source'] = 'https://github.com/jina-ai/jina-hub/commit/' + self.manifest['revision']
        print(print_purple(f"source: {self.manifest['source']}\n"))

    @staticmethod
    def get_modified_time(file_path) -> int:
        r = subprocess.check_output(['git', 'log', '-1', '--pretty=%at', str(file_path)]).strip().decode()
        if r:
            return int(r)
        else:
            print(print_red(f'Can\'t fetch the modified time of {file_path}, is it under git?'))
            return 0

    @staticmethod
    def get_hr_time(stamp):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))

    @staticmethod
    def get_canonic_name(target):
        return os.path.relpath(target).replace('/', '.').strip('.')


def print_purple(text):
    return '\033[35m' + str(text) + '\033[0m'


def print_green(text):
    return '\033[32m' + str(text) + '\033[0m'


def print_red(text):
    return '\033[31m' + str(text) + '\033[0m'
