""" Launcher functionality for the Azure Resource Manager (ARM)
"""
import json
import logging
import os
import subprocess
import tarfile
import tempfile
import uuid

import requests

import dcos_launch.util
import dcos_launch.platforms.arm

log = logging.getLogger(__name__)


def generate_acs_engine_template(
        linux_ssh_public_key: str,
        num_masters: int=1,
        master_vm_size: str='Standard_D2_V2',
        num_windows_private_agents: int=1,
        windows_private_vm_size: str='Standard_D2_V2',
        num_windows_public_agents: int=1,
        windows_public_vm_size: str='Standard_D2_V2',
        num_linux_private_agents: int=1,
        linux_private_vm_size: str='Standard_D2_V2',
        num_linux_public_agents: int=1,
        linux_public_vm_size: str='Standard_D2_V2',
        windows_admin_user: str='azureuser',
        windows_admin_password: str='replacepassword123',
        linux_admin_user: str='azureuser',
        ):
    unique_id = str(uuid.uuid4())[:8] + 'dcos'
    return {
        "apiVersion": "vlabs",
        "properties": {
            "orchestratorProfile": {
                "orchestratorType": "DCOS"
            },
            "masterProfile": {
                "count": num_masters,
                "dnsPrefix": "master" + unique_id,
                "vmSize": master_vm_size
            },
            "agentPoolProfiles": [
                {
                    "name": "wpub",
                    "count": num_windows_public_agents,
                    "vmSize": windows_public_vm_size,
                    "osType": "Windows",
                    "dnsPrefix": "wpub" + unique_id,
                    "ports": [80, 443, 8080, 3389]
                },
                {
                    "name": "wpri",
                    "count": num_windows_private_agents,
                    "vmSize": windows_private_vm_size,
                    "osType": "Windows",
                    "dnsPrefix": "wpri" + unique_id
                },
                {
                    "name": "linpub",
                    "count": num_linux_public_agents,
                    "vmSize": linux_public_vm_size,
                    "osType": "linux",
                    "dnsPrefix": "linpub" + unique_id,
                    "ports": [
                        80,
                        443,
                        22
                    ]
                },
                {
                    "name": "linpri",
                    "count": num_linux_private_agents,
                    "vmSize": linux_private_vm_size,
                    "osType": "linux",
                    "dnsPrefix": "linpri" + unique_id
                }
            ],
            "windowsProfile": {
                "adminUsername": windows_admin_user,
                "adminPassword": windows_admin_password
            },
            "linuxProfile": {
                "adminUsername": linux_admin_user,
                "ssh": {
                    "publicKeys": [
                        {
                            "keyData": linux_ssh_public_key
                        }
                    ]
                }
            }
        }
    }


def run_acs_engine(acs_engine_url: str, acs_engine_template):
    """ Runs the ACS engine
    """
    tmpdir = tempfile.mkdtemp()
    # pull down acs engine in temp dir
    download_path = os.path.join(tmpdir, 'download.tar.gz')
    with open(download_path, 'wb') as f:
        r = requests.get(acs_engine_url)
        for chunk in r.iter_content(1024):
            f.write(chunk)
    extract_path = os.path.join(tmpdir, 'extract')
    with tarfile.open(download_path) as tar:
        tar.extractall(path=extract_path)
    extracted_name = acs_engine_url.split('/')[-1].rstrip('.tar.gz')
    acs_engine_bin_path = os.path.join(extract_path, extracted_name, 'acs-engine')
    # inject parameters into the JSON (keyhelper, agent definitions)
    acs_template_path = os.path.join(tmpdir, 'acs_template.json')
    with open(acs_template_path, 'w') as f:
        json.dump(acs_engine_template, f)
    # run acs vs template
    cmd = [acs_engine_bin_path, 'generate', acs_template_path]
    subprocess.check_call(cmd, cwd=tmpdir)

    cluster_name = acs_engine_template['properties']['masterProfile']['dnsPrefix']
    with open(os.path.join(tmpdir, '_output/{}/azuredeploy.json'.format(cluster_name)), 'r') as f:
        arm_template = json.load(f)
    with open(os.path.join(tmpdir, '_output/{}/azuredeploy.parameters.json'.format(cluster_name)), 'r') as f:
        arm_template_parameters_raw = json.load(f)
    arm_template_parameters = dict()
    for k, v in arm_template_parameters_raw['parameters'].items():
        arm_template_parameters[k] = v['value']
    return arm_template, arm_template_parameters


class ACSEngineLauncher(dcos_launch.util.AbstractLauncher):
    def __init__(self, config: dict, env=None):
        if env is None:
            azure_subscription_id = dcos_launch.util.set_from_env('AZURE_SUBSCRIPTION_ID')
            azure_client_id = dcos_launch.util.set_from_env('AZURE_CLIENT_ID')
            azure_client_secret = dcos_launch.util.set_from_env('AZURE_CLIENT_SECRET')
            azure_tenant_id = dcos_launch.util.set_from_env('AZURE_TENANT_ID')
        else:
            azure_subscription_id = env['AZURE_SUBSCRIPTION_ID']
            azure_client_id = env['AZURE_CLIENT_ID']
            azure_client_secret = env['AZURE_CLIENT_SECRET']
            azure_tenant_id = env['AZURE_TENANT_ID']
        self.azure_wrapper = dcos_launch.platforms.arm.AzureWrapper(
            config['azure_location'],
            azure_subscription_id,
            azure_client_id,
            azure_client_secret,
            azure_tenant_id)
        self.config = config
        log.debug('Using Azure Resource Group Launcher')

    def create(self):
        if self.config['key_helper']:
            private_key, public_key = dcos_launch.util.generate_rsa_keypair()
            self.config.update({
                'ssh_private_key': private_key.decode(),
                'ssh_public_key': public_key.decode()})
        acs_engine_template = generate_acs_engine_template(
            self.config['ssh_public_key'],
            self.config['num_masters'],
            self.config['master_vm_size'],
            self.config['num_windows_private_agents'],
            self.config['windows_private_vm_size'],
            self.config['num_windows_public_agents'],
            self.config['windows_public_vm_size'],
            self.config['num_linux_private_agents'],
            self.config['linux_private_vm_size'],
            self.config['num_linux_public_agents'],
            self.config['linux_public_vm_size'],
            self.config['windows_admin_user'],
            self.config['windows_admin_password'],
            self.config['linux_admin_user'])
        arm_template, self.config['template_parameters'] = run_acs_engine(self.config['acs_engine_tarball_url'], acs_engine_template)  # noqa
        # FIXME: can we not pass this option into ACS-engine?
        # self.config['template_parameters']['oauthEnabled'] = "true"
        self.azure_wrapper.deploy_template_to_new_resource_group(
            self.config.get('template_url'),
            self.config['deployment_name'],
            self.config['template_parameters'],
            self.config.get('tags'),
            template=arm_template)
        return self.config

    def wait(self):
        self.resource_group.wait_for_deployment()

    def describe(self):
        return {
            'masters': dcos_launch.util.convert_host_list(self.resource_group.get_master_ips()),
            'private_agents': dcos_launch.util.convert_host_list(self.resource_group.get_private_agent_ips()),
            'public_agents': dcos_launch.util.convert_host_list(self.resource_group.get_public_agent_ips()),
            'master_fqdn': self.resource_group.public_master_lb_fqdn,
            'public_agent_fqdn': self.resource_group.public_agent_lb_fqdn}

    def delete(self):
        self.resource_group.delete()

    @property
    def resource_group(self):
        try:
            return dcos_launch.platforms.arm.DcosAzureResourceGroup(self.config['deployment_name'], self.azure_wrapper)
        except Exception as ex:
            raise dcos_launch.util.LauncherError('GroupNotFound', None) from ex
