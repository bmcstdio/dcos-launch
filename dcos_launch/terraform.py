import json
import logging
import os
import re
import shutil
import subprocess
import uuid
import zipfile
import requests
from dcos_test_utils import helpers
import dcos_launch.config
import yaml
from dcos_launch import gcp, util
from dcos_launch.platforms import aws

log = logging.getLogger(__name__)
IP_REGEX = '(\d{1,3}.){3}\d{1,3}'
IP_LIST_REGEX = '\[[^\]]+\]'


def _get_ips(prefix: str, info: str) -> list:
    ips = []
    prefix += ' = '
    m = re.search('{}{}'.format(prefix, IP_REGEX), info)
    if m:
        ips = [m.group(0)[len(prefix):]]
    else:
        m = re.search('{}{}'.format(prefix, IP_LIST_REGEX), info)
        if m:
            # remove prefix
            s = m.group(0)[len(prefix):]
            # remove whitespace
            s = "".join(s.split())
            # remove brackets
            s = s[1:-1]
            ips = s.split(',')
    return ips


def _convert_to_describe_format(ips: list) -> list:
    # TODO private ips not currently outputted in terraform-dcos
    return [{'private_ip': None, 'public_ip': ip} for ip in ips]


class TerraformLauncher(util.AbstractLauncher):
    def __init__(self, config: dict, env=None):
        if env:
            os.environ.update(env)
        self.config = config
        self.init_dir = dcos_launch.config.expand_path('', self.config['init_dir'])
        self.cluster_profile_path = os.path.join(self.init_dir, 'desired_cluster_profile.tfvars')
        self.dcos_launch_root_dir = os.path.abspath(os.path.join(self.init_dir, '..'))
        self.terraform_binary = os.path.join(self.dcos_launch_root_dir, 'terraform')
        self.default_priv_key_path = os.path.join(self.init_dir, 'key.pem')

    def terraform_cmd(self):
        """ Returns the right Terraform invocation command depending on whether it was installed by the user or by
        dcos-launch.
        """
        binary = self.terraform_binary
        if not os.path.exists(binary):
            binary = 'terraform'
        return binary

    def create(self):
        if os.path.exists(self.init_dir):
            raise util.LauncherError('ClusterAlreadyExists', "Either the cluster you are trying to create is already "
                                                             "running or the init_dir you specified in your config is "
                                                             "already used by another active cluster.")
        os.makedirs(self.init_dir)
        # Check if Terraform is installed by running 'terraform version'. If that fails, install Terraform.
        try:
            subprocess.run([self.terraform_cmd(), 'version'], check=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        except FileNotFoundError:
            log.info('No Terraform installation detected. Terraform is now being installed.')
            self._install_terraform()

        if self.config['key_helper']:
            self.key_helper()

        module = 'github.com/dcos/{}?ref={}/{}'.format(
            'terraform-dcos-enterprise' if self.config['dcos-enterprise'] else 'terraform-dcos',
            self.config['terraform_dcos_enterprise_version'] if self.config['dcos-enterprise'] else
            self.config['terraform_dcos_version'], self.config['platform'])

        # Converting our YAML config to the required format. You can find an example of that format in the
        # Advance YAML Configuration" section here:
        # https://github.com/mesosphere/terraform-dcos-enterprise/tree/master/aws
        with open(self.cluster_profile_path, 'w') as file:
            for k, v in self.config['terraform_config'].items():
                file.write(k + ' = ')
                if type(k) is dict:
                    file.write('<<EOF\n{}\nEOF\n'.format(yaml.dump(v)))
                else:
                    file.write('"{}"\n'.format(v))
        subprocess.run([self.terraform_cmd(), 'init', '-from-module', module], cwd=self.init_dir,
                       check=True, stderr=subprocess.STDOUT)
        cmd = [self.terraform_cmd(), 'apply', '-auto-approve', '-var-file', self.cluster_profile_path]
        ssh_cmd, shell = self._get_full_ssh_agent_cmd(['ssh-add', self.config['ssh_private_key_filename'], '&&'])
        cmd = ssh_cmd + cmd
        cmd = ' '.join(cmd) if shell else cmd
        subprocess.run(cmd, cwd=self.init_dir, check=True, stderr=subprocess.STDOUT, shell=shell,
                       env=os.environ)
        return self.config

    def _get_full_ssh_agent_cmd(self, cmd: list):
        if not self.config['key_helper']:
            return [], False
        shell = False
        if 'SSH_AUTH_SOCK' not in os.environ:
            cmd = ['eval', '`ssh-agent -s`', '&&'] + cmd
            shell = True
            log.info('No ssh-agent running. Starting one...')
        return cmd, shell

    def _install_terraform(self):
        download_path = os.path.join(self.dcos_launch_root_dir, 'terraform.zip')
        try:
            with open(download_path, 'wb') as f:
                log.info('Downloading...')
                r = requests.get(self.config['terraform_tarball_url'])
                for chunk in r.iter_content(1024):
                    f.write(chunk)
            with zipfile.ZipFile(download_path, 'r') as tfm_zip:
                tfm_zip.extractall(self.dcos_launch_root_dir)
            os.chmod(self.terraform_binary, 0o100)
        finally:
            os.remove(download_path)
        log.info('Terraform installation complete.')

    def wait(self):
        """ Nothing to do here because unlike the other launchers, create() runs and also waits for a subprocess to
        finish instead of just sending an http request to a provider.
        """
        pass

    def delete(self):
        # delete the cluster
        subprocess.run([self.terraform_cmd(), 'destroy', '-force', '-var-file', self.cluster_profile_path],
                       cwd=self.init_dir, check=True, stderr=subprocess.STDOUT, env=os.environ)
        # remove the key from the ssh-agent
        cmd, shell = self._get_full_ssh_agent_cmd(['ssh-add', '-d', self.config['ssh_private_key_filename']])
        cmd = ' '.join(cmd) if shell else cmd
        subprocess.run(cmd, check=False, stderr=subprocess.STDOUT, shell=shell)
        # remove the init dir
        shutil.rmtree(self.init_dir, ignore_errors=True)

    def describe(self) -> dict:
        """ Sample output from 'terraform output' command:
        Bootstrap Host Public IP = 35.227.147.39
        Master ELB Public IP = 35.230.64.74
        Master Public IPs = [
            35.230.56.247
        ]
        Private Agent Public IPs = [
            35.230.42.164,
            35.203.136.227
        ]
        Public Agent ELB Public IP = 35.230.71.57
        Public Agent Public IPs = [
            35.230.88.105
        ]
        ssh_user = core
        """
        result = subprocess.run([self.terraform_cmd(), 'output'], cwd=self.init_dir, check=True, stdout=subprocess.PIPE)
        info = result.stdout.decode('utf-8')
        self.config['ssh_user'] = re.search('ssh_user = \w+', info).group(0)[len('ssh_user = '):]

        private_agents_ips = _convert_to_describe_format(_get_ips('Private Agent Public IPs', info))
        private_agents_gpu_addresses = _get_ips('GPU Public IPs', info)
        for i in range(len(private_agents_gpu_addresses)):
            private_agents_ips[i]['GPU Public IPs'] = private_agents_gpu_addresses[i]

        description = {
            'bootstrap_host': _convert_to_describe_format(_get_ips('Bootstrap Host Public IP', info=info)),
            'masters': _convert_to_describe_format(_get_ips('Master Public IPs', info=info)),
            'private_agents': private_agents_ips,
            'public_agents': _convert_to_describe_format(_get_ips('Public Agent Public IPs', info))}

        master_elb_address = _get_ips('Master ELB Public IP', info=info)
        public_agent_elb_address = _get_ips('Public Agent ELB Public IP', info=info)

        if master_elb_address:
            description.update({'Master ELB Public IP': master_elb_address[0]})
        if public_agent_elb_address:
            description.update({'Public Agent ELB Public IP': public_agent_elb_address[0]})

        return description

    def key_helper(self):
        private_key, public_key = util.generate_rsa_keypair()
        self.config['ssh_private_key'] = private_key.decode('utf-8')
        with open(self.default_priv_key_path, 'wb') as f:
            f.write(private_key)
        os.chmod(self.default_priv_key_path, 0o600)
        self.config['ssh_private_key_filename'] = self.default_priv_key_path
        return public_key

    def test(self, args: list, env_dict: dict, test_host: str=None, test_port: int=22, details: dict=None) -> int:
        # TODO only reason this exists is because private IPs are not yet returned from describe(), which are required
        # by the parent test() function
        """ Connects to master host with SSH and then run the internal integration test

        Args:
            args: a list of args that will follow the py.test command
            env_dict: the env to use during the test
        """
        if args is None:
            args = list()
        if self.config['ssh_private_key'] == util.NO_TEST_FLAG or 'ssh_user' not in self.config:
            raise util.LauncherError('MissingInput', 'DC/OS Launch is missing sufficient SSH info to run tests!')
        if details is None:
            details = self.describe()
        # check for any environment variables that contain spaces
        env_dict = {e: "'{}'".format(env_dict[e]) if ' ' in env_dict[e] else env_dict[e] for e in env_dict}
        env_string = ' '.join(['{}={}'.format(e, env_dict[e]) for e in env_dict])
        arg_string = ' '.join(args)
        # To support 1.8.9-EE, try using the dcos-integration-test-ee folder if possible
        pytest_cmd = """ "source /opt/mesosphere/environment.export &&
cd `find /opt/mesosphere/active/ -name dcos-integration-test* | sort | tail -n 1` &&
{env} py.test {args}" """.format(env=env_string, args=arg_string)
        log.info('Running integration test...')
        if test_host is None:
            test_host = details['masters'][0]['public_ip']
        if ':' in test_host:
            test_host, test_port = test_host.split(':')
        env_dict['DCOS_DNS_ADDRESS'] = 'http://' + test_host
        return util.try_to_output_unbuffered(self.config, test_host, pytest_cmd, test_port)


class GcpLauncher(TerraformLauncher):
    def create(self):
        # if gcp region is nowhere to be found, the default value in terraform-dcos will be used
        if 'gcp_zone' not in self.config['terraform_config'] and 'GCE_ZONE' in os.environ:
            self.config['terraform_config']['gcp_zone'] = util.set_from_env('GCE_ZONE')
        if 'gcp_credentials_key_file' not in self.config['terraform_config']:
            creds_string, creds_path = gcp.get_credentials(os.environ)
            if not creds_path:
                creds_path = helpers.session_tempfile(creds_string)
            self.config['terraform_config']['gcp_credentials_key_file'] = creds_path
        if 'gcp_project' not in self.config['terraform_config']:
            with open(self.config['terraform_config']['gcp_credentials_key_file']) as f:
                self.config['terraform_config']['gcp_project'] = json.load(f)['project_id']
        return super().create()

    def key_helper(self):
        if 'gcp_ssh_pub_key_file' not in self.config['terraform_config'] or \
                'ssh_private_key_filename' not in self.config:
            pub_key = super().key_helper()
            pub_key_file = os.path.join(self.init_dir, 'key.pub')
            with open(pub_key_file, 'wb') as f:
                f.write(pub_key)
            self.config['terraform_config']['gcp_ssh_pub_key_file'] = pub_key_file


class AzureLauncher(TerraformLauncher):
    def create(self):
        dcos_launch.util.set_from_env('ARM_SUBSCRIPTION_ID')
        dcos_launch.util.set_from_env('ARM_CLIENT_ID')
        dcos_launch.util.set_from_env('ARM_CLIENT_SECRET')
        dcos_launch.util.set_from_env('ARM_TENANT_ID')
        # if azure region is nowhere to be found, the default value in terraform-dcos will be used
        if 'azure_region' not in self.config['terraform_config'] and 'AZURE_LOCATION' in os.environ:
            self.config['terraform_config']['azure_region'] = util.set_from_env('AZURE_LOCATION')
        return super().create()

    def key_helper(self):
        if 'ssh_pub_key' not in self.config['terraform_config'] or \
                'ssh_private_key_filename' not in self.config:
            pub_key = super().key_helper()
            self.config['terraform_config']['ssh_pub_key'] = pub_key.decode('utf-8')


class AwsLauncher(TerraformLauncher):
    def key_helper(self):
        if 'ssh_key_name' not in self.config['terraform_config'] or \
                'ssh_private_key_filename' not in self.config:
            bw = aws.BotoWrapper(self.config['aws_region'])
            key_name = 'terraform-dcos-launch-' + str(uuid.uuid4())
            private_key = bw.create_key_pair(key_name)
            with open(self.default_priv_key_path, 'wb') as f:
                f.write(private_key.encode())
            os.chmod(self.default_priv_key_path, 0o600)
            self.config['ssh_private_key_filename'] = self.default_priv_key_path
            self.config['terraform_config']['ssh_key_name'] = key_name
