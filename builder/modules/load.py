import os
import json

import datetime
import re
import requests
import subprocess

from pymongo import MongoClient
from ruamel.yaml import YAML
from builder.color_print import *
yaml = YAML()

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
build_hist_path = os.path.join(root_dir, 'api', 'hub', 'build.json')


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

    def update_history_on_db(self, **kwargs):
        spec = {'_id': 1}
        self.db.docker.replace_one(filter=spec, replacement=dict(**kwargs), upsert=True)

    def get_history_from_database(self):
        spec = {'_id': 1}
        return self.db.docker.find_one(filter=spec)

    # def select_head(self, n):
    #     return dict(self.db.docker.find().limit(n))
    #
    # def contains(self, item):  #  -> int | bool
    #     return dict(self.db.docker.find(item).count())


class StateLoader(Mongo):

    def __init__(self, error_on_empty=False):
        Mongo.__init__(self)
        self.error_on_empty = error_on_empty

    def get_history(self):
        local_history = self.get_local_history()
        remote_history = self.get_history_from_database()
        empty_history = {'Images': {}, 'LastBuildTime': {}, 'LastBuildStatus': {}, 'LastBuildReason': ''}
        history = remote_history or local_history or empty_history
        if history == empty_history:
            print(print_red('\nCan\'t load build history from database or ') + build_hist_path)
        return history

    @staticmethod
    def get_local_history():
        if os.path.isfile(build_hist_path):
            with open(build_hist_path, 'r') as fp:
                history = json.load(fp)
            return history

    @staticmethod
    def get_maps(history):
        image_map = history.get('Images', {})
        status_map = history.get('LastBuildStatus', {})
        last_build_time = history.get('LastBuildTime', {})
        return image_map, status_map, last_build_time

    def update_total_history(self, history):
        self.update_readme(history)
        self.update_hub_badge(history)
        self.update_json_track(history)

    def update_readme(self, history):
        readme_path = os.path.join(root_dir, 'status', 'README.md')
        build_badge_regex = r'<!-- START_BUILD_BADGE -->(.*)<!-- END_BUILD_BADGE -->'
        build_badge_prefix = r'<!-- START_BUILD_BADGE --><!-- END_BUILD_BADGE -->'
        with open(readme_path, 'r') as fp:
            tmp = fp.read()
            badge_str = '\n'.join([self.get_badge_md(k, v) for k, v in history['LastBuildStatus'].items()])
            h1 = f'## Last Build at: {datetime.datetime.now():%Y-%m-%d %H:%M:%S %Z}'
            h2 = '<summary>Reason</summary>'
            h3 = '**Images**'
            content = [build_badge_prefix, h1, h3, badge_str, '<details>', h2, history['LastBuildReason'], '</details>']
            tmp = re.sub(pattern=build_badge_regex, repl='\n\n'.join(content), string=tmp, flags=re.DOTALL)
        with open(readme_path, 'w') as fp:
            fp.write(tmp)
            print(print_green('Hub readme updated successfully on path ') + str(readme_path))

    @staticmethod
    def get_badge_md(img_name, status):
        safe_url_name = img_name.replace('-', '--').replace('_', '__').replace(' ', '_')
        if status == 'success':
            success_tag = 'success-success'
        elif status == 'fail':
            success_tag = 'fail-critical'
        else:
            success_tag = 'pending-yellow'
        return f'[![{img_name}](https://img.shields.io/badge/{safe_url_name}-' \
               f'{success_tag}?style=flat-square)]' \
               f'(https://hub.docker.com/repository/docker/jinaai/{img_name})'

    def update_json_track(self, history):
        builder_revision = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).strip().decode()
        history.update({'BuilderRevision': builder_revision})
        with open(build_hist_path, 'w') as fp:
            json.dump(history, fp)
        print(print_green('Hub history updated successfully on path ') + str(build_hist_path))

        self.update_history_on_db(**history)
        print(print_green('Hub history updated successfully on database'))

    @staticmethod
    def update_hub_badge(history):
        hubbadge_path = os.path.join(root_dir, 'status', 'hub-stat.svg')
        url = f'https://badgen.net/badge/Hub%20Images/{len(history["Images"])}/cyan'
        response = requests.get(url)
        if response.ok:
            with open(hubbadge_path, 'wb') as opfile:
                opfile.write(response.content)
            print(print_green('Hub badge updated successfully on path ') + str(hubbadge_path))
        else:
            print(print_red('Hub badge update failed ') + str(hubbadge_path))
