import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import unicodedata
import string
from datetime import datetime
import time
from pathlib import Path

from jina.flow import Flow
from ruamel.yaml import YAML

yaml = YAML()
allowed = {'name', 'description', 'author', 'url', 'documentation', 'version', 'vendor', 'license', 'avatar',
           'platform'}
required = {'name', 'description'}
sver_regex = r'^(=|>=|<=|=>|=<|>|<|!=|~|~>|\^)?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)' \
             r'\.(?P<patch>0|[1-9]\d*)(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)' \
             r'(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+' \
             r'(?:\.[0-9a-zA-Z-]+)*))?$'
name_regex = r'^[a-zA-Z_$][a-zA-Z_\s\-$0-9]{2,20}$'

cur_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

jinasrc_dir = os.path.join(root_dir, 'src', 'jina')
image_tag_regex = r'^hub.[a-zA-Z_$][a-zA-Z_\s\-\.$0-9]*$'
label_prefix = 'ai.jina.hub.'
docker_registry = 'jinaai/'

# current date and time
builder_files = list(Path(root_dir).glob('builder/app.py')) + \
                list(Path(root_dir).glob('builder/*.yml'))

build_hist_path = os.path.join(root_dir, 'status', 'build-history.json')
readme_path = os.path.join(root_dir, 'status', 'README.md')
hubbadge_path = os.path.join(root_dir, 'status', 'hub-stat.svg')

hub_files = list(Path(root_dir).glob('hub/**/*.y*ml')) + \
            list(Path(root_dir).glob('hub/**/*Dockerfile')) + \
            list(Path(root_dir).glob('hub/**/*.py'))

builder_revision = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).strip().decode()
build_badge_regex = r'<!-- START_BUILD_BADGE -->(.*)<!-- END_BUILD_BADGE -->'
build_badge_prefix = r'<!-- START_BUILD_BADGE --><!-- END_BUILD_BADGE -->'


class Validator:

    def __init__(self, manifest=None, image_canonical_name=''):
        self.manifest = manifest
        self.image_canonical_name = image_canonical_name

    def validate_schema(self):
        keys = set(self.manifest.keys())
        if len(required - keys) > 0:
            raise ValueError(f"{required - keys} required!")
        elif len(keys - allowed) > 0:
            raise ValueError(f"{keys - allowed} are not allowed!")

    def check_chain(self):
        self.check_image_canonical_name()
        self.check_name()
        self.check_version()
        self.check_license()
        self.check_platform()

    def check_image_canonical_name(self):
        if not re.match(image_tag_regex, self.image_canonical_name):
            raise ValueError(
                f'{self} is not a valid image name for a Jina Hub image, it should match with {image_tag_regex}'
            )

    def check_name(self):
        name = self.manifest.get('name')
        if not re.match(name_regex, name):
            raise ValueError(f'{name} is not a valid name, it should match with {name_regex}')

    def check_version(self):
        version = self.manifest.get('version')
        if not re.match(sver_regex, version):
            raise ValueError(f'{version} is not a valid semantic version number, see http://semver.org/')

    def check_license(self):
        license_ = self.manifest.get('license')
        with open(os.path.join(cur_dir, 'osi-approved.yml')) as fp:
            approved = yaml.load(fp)
        if license_ not in approved:
            raise ValueError(f"license {license_} is not an OSI-approved license {approved}")
        return approved[license_]

    def check_platform(self):
        with open(os.path.join(cur_dir, 'platforms.yml')) as yml:
            supported_platforms = yaml.load(yml)

        for user_added_platform in self.manifest.get('platform'):
            if user_added_platform not in supported_platforms:
                raise ValueError(
                    f'platform {user_added_platform} is not supported, should be one of {supported_platforms}'
                )

    @staticmethod
    def check_image_in_hub(img_name):
        subprocess.check_call(['docker', 'pull', img_name])



class SingleBuilder(Validator):

    def __init__(self, args):
        super().__init__()
        self.target: str = args.target
        self.reason: str = args.reason
        self.push: bool = args.push
        self.test: bool = args.test
        self.error_on_empty: bool = args.error_on_empty
        self.check_only: bool = args.check_only
        self.bleach_first: bool = args.bleach_first

        self.dockerfile_path = None
        self.manifest_path = None
        self.image_canonical_name = None
        self.manifest = None

    def run(self):
        if self.construct_paths():
            self.load_manifest()
            self.validate_schema()
            self.update_fields()
            self.check_chain()

            self.img_name = self.build_image()
            self.pushing()

            self.check_image_in_hub(self.img_name)
            # self.img_name = f'{docker_registry}{self.image_canonical_name}:{self.manifest["version"]}'
            self.testing(self.img_name)

    def construct_paths(self):
        if os.path.isdir(self.target):
            self.dockerfile_path = os.path.join(self.target, 'Dockerfile')
            self.manifest_path = os.path.join(self.target, 'manifest.yml')
            self.image_canonical_name = get_canonic_name(self.target)
            return True
        elif self.error_on_empty:
            raise NotADirectoryError(f'{self.target} is not a valid directory')

    def load_manifest(self):
        with open(self.manifest_path) as yml:
            self.manifest = yaml.load(yml)

    def update_fields(self):
        for key, value in self.manifest.items():
            updated_value = self.remove_control_characters(value)
            if updated_value != value:
                self.manifest[key] = updated_value
                print('\033[35m' + f'{key}: {value} -> {updated_value}' + '\033[0m' + ' // removed invalid chars')
            else:
                print('\033[35m' + f'{key}: {value}' + '\033[0m')
        self.add_platform_revision_source()

    @staticmethod
    def remove_control_characters(source):
        return ''.join(ch for ch in source if not unicodedata.category(ch).startswith('C'))

    def add_platform_revision_source(self):
        self.manifest['platform'] = self.manifest.get('platform') or []
        print('\033[35m' + f"platform: {self.manifest['platform'] or 'Undefined'}" + '\033[0m')

        self.manifest['revision'] = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).strip().decode()
        print('\033[35m' + f"revision: {self.manifest['revision']}" + '\033[0m')

        self.manifest['source'] = 'https://github.com/jina-ai/jina-hub/commit/' + self.manifest['revision']
        print('\033[35m' + f"source: {self.manifest['source']}\n" + '\033[0m')

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
        print('\033[32m' + f'\nDockerfile on ' + '\033[0m' + f'{self.dockerfile_path}:\n')
        for line in revised_dockerfile:
            if line != '\n':
                print('\033[35m' + line + '\033[35m')
        print()

        with open(self.dockerfile_path + '.tmp', 'w') as fp:
            fp.writelines(revised_dockerfile)

        shutil.copytree(src=jinasrc_dir, dst=os.path.join(self.target, 'jina'))

        dockerbuild_cmd = ['docker', 'buildx', 'build']

        image_name = f'{docker_registry}{self.image_canonical_name}:{self.manifest["version"]}'
        tag = f'{docker_registry}{self.image_canonical_name}:latest'
        dockerbuild_args = [
            '-t', image_name,
            '-t', tag,
            '--file', self.dockerfile_path + '.tmp'
        ]
        dockerbuild_platform = ['--platform', ','.join(v for v in self.manifest['platform'])] \
            if self.manifest['platform'] else []
        dockerbuild_action = '--push' if self.push else '--load'
        docker_cmd = dockerbuild_cmd + dockerbuild_platform + dockerbuild_args + [dockerbuild_action, self.target]
        subprocess.check_call(docker_cmd)

        print('\033[34m' + 'Building finished successfully!' + '\033[0m')
        return image_name

    def testing(self, img_name):
        if self.test:
            print('\033[32m' + 'testing image with docker run' + '\033[0m')
            subprocess.check_call(['docker', 'run', '--rm', img_name, '--max-idle-time', '5', '--shutdown-idle'])

            print('\033[32m' + 'testing image with jina cli' + '\033[0m')
            subprocess.check_call(['jina', 'pod', '--image', img_name, '--max-idle-time', '5', '--shutdown-idle'])

            print('\033[32m' + 'testing image with jina flow API' + '\033[0m')
            with Flow().add(image=img_name, replicas=3).build():
                pass
            print('\033[34m' + 'All tests passed successfully!' + '\033[0m')

    def pushing(self):
        if self.push:
            target_readme_path = os.path.join(self.target, 'README.md')
            if not os.path.exists(target_readme_path):
                with open(target_readme_path, 'w') as fp:
                    fp.write('#{name}\n\n#{description}\n'.format_map(self.manifest))

            docker_readme_cmd = ['docker', 'run', '-v', f'{self.target}:/workspace',
                                 '-e', f'DOCKERHUB_USERNAME={os.environ["DOCKERHUB_DEVBOT_USER"]}',
                                 '-e', f'DOCKERHUB_PASSWORD={os.environ["DOCKERHUB_DEVBOT_PWD"]}',
                                 '-e', f'DOCKERHUB_REPOSITORY={docker_registry}{self.image_canonical_name}',
                                 '-e', 'README_FILEPATH=/workspace/README.md',
                                 'peterevans/dockerhub-description:2.1']
            subprocess.check_call(docker_readme_cmd)
            print('\033[34m' + 'Readme upload finished successfully!' + '\033[0m')


class MultiBuilder(SingleBuilder):

    def __init__(self, args):
        super().__init__(args)
        # self.target: str = args.target #  ТОЧНО НЕ НУЖЕН

        # self.reason: str = args.reason #  ВЕРОЯТНО МОЖНО УДАЛИТЬ, Т.К инициируются в родительском классе
        # self.push: bool = args.push
        # self.test: bool = args.test
        # self.error_on_empty: bool = args.error_on_empty
        # self.check_only: bool = args.check_only
        # self.bleach_first: bool = args.bleach_first

        self.image_map = {}
        self.status_map = {}
        self.last_build_time = {}
        self.last_builder_update_timestamp = 0

        self.img_name = ''

    def run(self):
        self.load_build_history()
        self.load_builder_update_history()
        update_targets, is_builder_updated = self.get_targets()

        if update_targets:
            if is_builder_updated:
                self.set_reason(targets=update_targets, reason='builder was updated')
            else:
                self.set_reason(targets=update_targets, reason='manual update')
            self.build_factory(targets=update_targets)
            self.update_readme()
        else:
            self.set_reason(targets=None, reason='empty target set')
            print('\033[34m' + 'Noting to build' + '\033[0m')

    def load_build_history(self):
        try:
            with open(build_hist_path, 'r') as fp:
                hist = json.load(fp)
        except FileNotFoundError:
            print('\033[32m' + '\nCan\'t fetch "LastBuildTime" from build-history.json' + '\033[0m')
            print('\033[32m' + 'Initiating new one ...\n' + '\033[0m')
            hist = {}

        self.image_map = hist.get('Images', {})
        self.status_map = hist.get('LastBuildStatus', {})
        self.last_build_time = hist.get('LastBuildTime', {})
        print(f'last build time: {self.last_build_time}')

    def load_builder_update_history(self):
        for file_path in builder_files:
            updated_timestamp = self.get_modified_time(file_path)
            if updated_timestamp > self.last_builder_update_timestamp:
                self.last_builder_update_timestamp = updated_timestamp
        print(f'last builder update: {time.strftime("%D %H:%M", time.localtime(self.last_builder_update_timestamp))}\n')

    @staticmethod
    def get_modified_time(file_path) -> int:
        r = subprocess.check_output(['git', 'log', '-1', '--pretty=%at', str(file_path)]).strip().decode()
        if r:
            return int(r)
        else:
            print('\033[31m' + f'Can\'t fetch the modified time of {file_path}, is it under git?' + '\033[0m')
            return 0

    def get_targets(self):
        update_targets = set()
        is_builder_updated = False

        for file_path in hub_files:
            modified_time = self.get_modified_time(file_path)
            target = os.path.dirname(os.path.abspath(file_path))
            canonic_name = get_canonic_name(target)
            last_image_build_time = self.last_build_time.get(canonic_name, 0)

            is_target_to_be_added = False
            if self.last_builder_update_timestamp > last_image_build_time:
                is_builder_updated = True
                is_target_to_be_added = True
            elif modified_time > last_image_build_time:
                is_target_to_be_added = True

            if is_target_to_be_added:
                update_targets.add(target)
                print(f'{file_path} is added')
                print(f'last_builder_update: {time.strftime("%D %H:%M", time.localtime(self.last_builder_update_timestamp))}')
                print(f'last_image_build_time: {last_image_build_time or "---"}')
                print(f'modified_time: {time.strftime("%D %H:%M", time.localtime(modified_time))}\n')

        return update_targets, is_builder_updated

    def build_factory(self, targets):
        for target in targets:
            self.target = target
            canonic_name = get_canonic_name(target)
            print('\033[32m' + f'Building image {canonic_name}\n' + '\033[0m')
            self.status_map[canonic_name] = 'pending'

            try:
                super().run()
                docker_inspect_output = subprocess.check_output(['docker', 'inspect', self.img_name]).strip().decode()
                tmp = json.loads(docker_inspect_output)[0]

                if canonic_name not in self.image_map:
                    self.image_map[canonic_name] = []
                self.image_map[canonic_name].append({
                    'Status': True,
                    'LastBuildTime': int(time.time()),
                    'Inspect': tmp,
                })
                self.status_map[canonic_name] = 'success'
            except Exception as ex:
                self.status_map[canonic_name] = 'fail'
                print(ex)

    def update_readme(self):
        with open(readme_path, 'r') as fp:
            tmp = fp.read()
            badge_str = '\n'.join([self.get_badge_md(k, v) for k, v in self.status_map.items()])
            h1 = f'## Last Build at: {datetime.now():%Y-%m-%d %H:%M:%S %Z}'
            h2 = '<summary>Reason</summary>'
            h3 = '**Images**'
            reason = '\n\n'.join([v for v in self.reason]) ## WHAT THE HELL IS THIS
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


def get_canonic_name(target):
    return os.path.relpath(target).replace('/', '.')[3:]


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', type=str,
                        help='the directory path of target Pod image, where manifest.yml and Dockerfile located')
    gp1 = parser.add_mutually_exclusive_group()
    gp1.add_argument('--push', action='store_true', default=False,
                     help='push to the registry')
    gp1.add_argument('--test', action='store_true', default=False,
                     help='test the pod image')
    parser.add_argument('--error-on-empty', action='store_true', default=False,
                        help='stop and raise error when the target is empty, otherwise just gracefully exit')
    parser.add_argument('--reason', type=str, nargs='*',
                        help='the reason of the build')
    parser.add_argument('--check-only', action='store_true', default=False,
                        help='check if the there is anything to update')
    parser.add_argument('--bleach-first', action='store_true', default=False,
                        help='clear docker before starting the build')
    return parser


def clean_docker():
    print('\033[32m' + 'Removing all existing docker instances' + '\033[0m')
    for k in ['df -h',
              'docker stop $(docker ps -aq)',
              'docker rm $(docker ps -aq)',
              'docker rmi -f $(docker image ls -aq)',
              'df -h']:
        try:
            subprocess.check_call(k, shell=True)
        except subprocess.CalledProcessError:
            pass

if __name__ == '__main__':
    args = get_parser().parse_args()
    if args.bleach_first:
        clean_docker()
    if args.check_only:
        pass
        # t = get_update_targets()[0]
        # if t:
        #     exit(0)
        # else:
        #     # nothing to update exit with 1
        #     exit(1)
    if args.target:
        builder = SingleBuilder(args)
    else:
        builder = MultiBuilder(args)
    builder.run()
