import os
import re

from ruamel.yaml import YAML
yaml = YAML()


class Validator:

    def __init__(self, target):
        self.target = target
        self.check_chain()

    def check_chain(self):
        self.check_name()
        self.check_version()
        self.check_license()
        self.check_platform()

    def check_name(self):
        name_regex = r'^[a-zA-Z_$][a-zA-Z_\s\-$0-9]{2,20}$'
        name = self.target.manifest['name']
        if not re.match(name_regex, name):
            raise ValueError(f'{name} is not a valid name, it should match with {name_regex}')

    def check_version(self):
        sver_regex = r'^(=|>=|<=|=>|=<|>|<|!=|~|~>|\^)?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)' \
                     r'\.(?P<patch>0|[1-9]\d*)(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)' \
                     r'(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+' \
                     r'(?:\.[0-9a-zA-Z-]+)*))?$'
        version = self.target.manifest.get('version', {})
        if not re.match(sver_regex, version):
            raise ValueError(f'{version} is not a valid semantic version number, see http://semver.org/')

    def check_license(self):
        cur_dir = os.path.dirname(os.path.abspath(__file__))
        osi_approved_yml_path = os.path.join(os.path.dirname(cur_dir), 'osi-approved.yml')
        license_ = self.target.manifest.get('license', {})
        with open(osi_approved_yml_path) as fp:
            approved = yaml.load(fp)
        if license_ not in approved:
            raise ValueError(f"license {license_} is not an OSI-approved license {approved}")
        return approved[license_]

    def check_platform(self):
        cur_dir = os.path.dirname(os.path.abspath(__file__))
        platforms_yml_path = os.path.join(os.path.dirname(cur_dir), 'platforms.yml')
        platforms = self.target.manifest.get('platform', {})
        with open(platforms_yml_path) as yml:
            supported_platforms = yaml.load(yml)

        for user_added_platform in platforms:
            if user_added_platform not in supported_platforms:
                raise ValueError(
                    f'platform {user_added_platform} is not supported, should be one of {supported_platforms}'
                )

