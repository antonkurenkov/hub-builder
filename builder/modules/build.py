from builder.modules.valid import Validator
from builder.modules.load import Loader

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
import time
import requests

from jina.flow import Flow
from ruamel.yaml import YAML

yaml = YAML()

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
jinasrc_dir = os.path.join(root_dir, 'src', 'jina')
build_hist_path = os.path.join(root_dir, 'api', 'hub', 'build.json')
status_path = os.path.join(root_dir, 'api', 'hub', 'status.json')
readme_path = os.path.join(root_dir, 'status', 'README.md')
hubbadge_path = os.path.join(root_dir, 'status', 'hub-stat.svg')

image_tag_regex = r'^hub.[a-zA-Z_$][a-zA-Z_\s\-\.$0-9]*$'
label_prefix = 'ai.jina.hub.'
docker_registry = 'jinaai/'

builder_revision = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).strip().decode()
build_badge_regex = r'<!-- START_BUILD_BADGE -->(.*)<!-- END_BUILD_BADGE -->'
build_badge_prefix = r'<!-- START_BUILD_BADGE --><!-- END_BUILD_BADGE -->'


class SingleBuilder(Validator, Loader):

    def __init__(self, args):
        Validator.__init__(self)

        self.bleach_first: bool = args.bleach_first
        self.check_targets: bool = args.check_targets
        self.push: bool = args.push
        self.test: bool = args.test
        self.reason: str = args.reason

    def run(self):
        if self.construct_paths():
            self.load_manifest()
            self.validate_schema()
            self.load_fields_from_manifest()
            self.check_chain()
            self.img_name = self.build_image()
            self.pushing()
            self.check_image_in_hub(self.img_name)
            self.testing(self.img_name)

    def build_image(self):
        revised_dockerfile = []
        with open(self.dockerfile_path) as dockerfile:
            for line in dockerfile:
                revised_dockerfile.append(line)
                if line.startswith('FROM'):
                    revised_dockerfile.append('LABEL ')
                    revised_dockerfile.append(
                        ' \\      \n'.join(f'{label_prefix}{k}="{v}"' for k, v in self.manifest.items())
                    )
        print(print_green('Dockerfile on ') + self.dockerfile_path)
        for line in revised_dockerfile:
            if line != '\n':
                print(print_purple(line.strip('\n')))

        with open(self.dockerfile_path + '.tmp', 'w') as fp:
            fp.writelines(revised_dockerfile)

        if not os.path.isdir(os.path.join(self.target, 'jina')):
            shutil.copytree(src=jinasrc_dir, dst=os.path.join(self.target, 'jina'))

        dockerbuild_cmd = ['docker', 'buildx', 'build']

        image_name = f'{docker_registry}{self.canonic_name}:{self.manifest["version"]}'
        tag = f'{docker_registry}{self.canonic_name}:latest'
        dockerbuild_args = [
            '-t', image_name,
            '-t', tag,
            '--file', self.dockerfile_path + '.tmp'
        ]
        dockerbuild_platform = ['--platform', ','.join(v for v in self.manifest['platform'])] \
            if self.manifest['platform'] else []
        dockerbuild_action = '--push' if self.push else '--load'
        docker_cmd = dockerbuild_cmd + dockerbuild_platform + dockerbuild_args + [dockerbuild_action, self.target]

        print(print_green('Starting docker build for image ') + self.canonic_name + '\n')
        subprocess.check_call(docker_cmd)

        print(print_green('Successfully built image ') + self.canonic_name + '\n')
        self.last_build_time[self.canonic_name] = int(time.time())
        return image_name

    def testing(self, img_name):
        if self.test:
            print(print_green('Testing image with docker run...'))
            subprocess.check_call(['docker', 'run', '--rm', img_name, '--max-idle-time', '5', '--shutdown-idle'])

            print(print_green('Testing image with jina cli...'))
            subprocess.check_call(['jina', 'pod', '--image', img_name, '--max-idle-time', '5', '--shutdown-idle'])

            print(print_green('Testing image with jina flow API...'))
            with Flow().add(image=img_name, replicas=3).build():
                pass
            print(print_green('All tests passed successfully!'))

    def pushing(self):
        if self.push:
            target_readme_path = os.path.join(self.target, 'README.md')
            if not os.path.exists(target_readme_path):
                with open(target_readme_path, 'w') as fp:
                    fp.write('#{name}\n\n#{description}\n'.format_map(self.manifest))

            docker_readme_cmd = ['docker', 'run', '-v', f'{self.target}:/workspace',
                                 '-e', f'DOCKERHUB_USERNAME={os.environ["DOCKERHUB_DEVBOT_USER"]}',
                                 '-e', f'DOCKERHUB_PASSWORD={os.environ["DOCKERHUB_DEVBOT_TOKEN"]}',
                                 '-e', f'DOCKERHUB_REPOSITORY={docker_registry}{self.canonic_name}',
                                 '-e', 'README_FILEPATH=/workspace/README.md',
                                 'peterevans/dockerhub-description:2.1']
            subprocess.check_call(docker_readme_cmd)
            print(print_green('Readme upload finished successfully!'))

    @staticmethod
    def check_image_in_hub(img_name):
        print(print_green('Pulling image ') + img_name)
        subprocess.check_call(['docker', 'pull', img_name])


class MultiBuilder(SingleBuilder, Loader):

    def __init__(self, args):
        SingleBuilder.__init__(self, args)
        Loader.__init__(self, args)

    def run(self):
        self.load_build_history()
        self.load_builder_update_history()

        update_targets, is_builder_updated = self.get_targets()
        if update_targets:
            if not self.check_targets:
                if is_builder_updated:
                    self.set_reason(targets=update_targets, reason='builder was updated')
                else:
                    self.set_reason(targets=update_targets, reason='manual update')
                built_num = self.build_factory(targets=update_targets)
                self.update_readme()
                self.update_json_track()
                self.update_hub_badge()
                if built_num == len(update_targets):
                    print(print_green('Delivered ') + f'{built_num}/{len(update_targets)}')
                else:
                    print(print_red('Delivered ') + f'{built_num}/{len(update_targets)}')
        else:
            print(print_green('Noting to build'))
            exit(1)

    def build_factory(self, targets):
        success = 0
        for i, target in enumerate(targets):

            self.target = target
            self.canonic_name = self.get_canonic_name(self.target)
            print(print_green(f'\nImage ({i + 1}/{len(targets)}): ') + self.canonic_name)
            self.status_map[self.canonic_name] = 'pending'
            try:
                super().run()
                docker_inspect_output = subprocess.check_output(['docker', 'inspect', self.img_name]).strip().decode()
                tmp = json.loads(docker_inspect_output)[0]

                if self.canonic_name not in self.image_map:
                    self.image_map[self.canonic_name] = []
                self.image_map[self.canonic_name].append({
                    'Status': True,
                    'LastBuildTime': int(time.time()),
                    'Inspect': tmp,
                })
                self.status_map[self.canonic_name] = 'success'
                print(print_green('Successfully delivered image ') + self.canonic_name)
                success += 1

            except Exception as ex:
                self.status_map[self.canonic_name] = 'fail'
                print(print_red(ex) + f' while delivering image {self.canonic_name}')

        return success

    @staticmethod
    def get_canonic_name(target):
        return os.path.relpath(target).replace('/', '.').strip('.')

    def update_readme(self):
        with open(readme_path, 'r') as fp:
            tmp = fp.read()
            badge_str = '\n'.join([self.get_badge_md(k, v) for k, v in self.status_map.items()])
            h1 = f'## Last Build at: {datetime.now():%Y-%m-%d %H:%M:%S %Z}'
            h2 = '<summary>Reason</summary>'
            h3 = '**Images**'
            reason = ''.join([v for v in self.reason])
            tmp = re.sub(
                pattern=build_badge_regex,
                repl='\n\n'.join([build_badge_prefix, h1, h3, badge_str, '<details>', h2, reason, '</details>']),
                string=tmp,
                flags=re.DOTALL
            )
        with open(readme_path, 'w') as fp:
            fp.write(tmp)

    def get_badge_md(self, img_name, status):
        if status == 'success':
            success_tag = 'success-success'
        elif status == 'fail':
            success_tag = 'fail-critical'
        else:
            success_tag = 'pending-yellow'
        return f'[![{img_name}](https://img.shields.io/badge/{self.safe_url_name(img_name)}-' \
               f'{success_tag}?style=flat-square)]' \
               f'(https://hub.docker.com/repository/docker/jinaai/{img_name})'

    @staticmethod
    def safe_url_name(s):
        return s.replace('-', '--').replace('_', '__').replace(' ', '_')

    def set_reason(self, targets, reason):
        self.reason = f'{targets} updated due to {reason}.'

    def update_json_track(self, local=True, db=True):
        data = {
            'LastBuildTime': self.last_build_time,
            'LastBuildReason': self.reason,
            'LastBuildStatus': self.status_map,
            'BuilderRevision': builder_revision,
            'Images': self.image_map,
        }

        if local:
            if not os.path.isdir(os.path.dirname(build_hist_path)):
                os.mkdir(os.path.join(root_dir, 'api', 'hub'))
            with open(build_hist_path, 'w') as fp:
                json.dump(data, fp)
                print(print_green('History updated successfully on path ') + str(build_hist_path))
        if db:
            self.create_or_update(**data)
            print(print_green('History updated successfully on database'))

    def update_hub_badge(self):
        url = f'https://badgen.net/badge/Hub%20Images/{len(self.image_map)}/cyan'
        response = requests.get(url)
        if response.ok:
            with open(hubbadge_path, 'wb') as opfile:
                opfile.write(response.content)
            print(print_yellow('Badge updated successfully on path ') + str(hubbadge_path))
        else:
            print(print_red('Badge update failed ') + str(hubbadge_path))


def print_green(text):
    return '\033[32m' + str(text) + '\033[0m'


def print_yellow(text):
    return '\033[33m' + str(text) + '\033[0m'


def print_red(text):
    return '\033[31m' + str(text) + '\033[0m'


def print_purple(text):
    return '\033[35m' + str(text) + '\033[0m'
