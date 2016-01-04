import functools
import string
import sys
import time
import urllib.request
from collections import namedtuple
from datetime import datetime

# External modules
import boto
import boto.ec2
import click

# Flintrock modules
from .core import FlintrockCluster
from .core import provision_cluster
from .exceptions import (
    ClusterNotFound,
    ClusterAlreadyExists,
    ClusterInvalidState,
    NothingToDo)
from .ssh import generate_ssh_key_pair


def timeit(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = datetime.now().replace(microsecond=0)
        res = func(*args, **kwargs)
        end = datetime.now().replace(microsecond=0)
        print("{f} finished in {t}.".format(f=func.__name__, t=(end - start)))
        return res
    return wrapper


class EC2Cluster(FlintrockCluster):
    def __init__(
            self,
            region: str,
            master_instance: boto.ec2.instance.Instance,
            slave_instances: list,
            *args,
            **kwargs):
        super().__init__(*args, **kwargs)
        self.region = region
        self.master_instance = master_instance
        self.slave_instances = slave_instances

    # TODO: Should master/slave _ip/_hostname be dynamically derived
    #       from the instances?

    @property
    def instances(self):
        return [self.master_instance] + self.slave_instances

    @property
    def state(self):
        instance_states = set(
            instance.state for instance in self.instances)
        if len(instance_states) == 1:
            return instance_states.pop()
        else:
            return 'inconsistent'

    def wait_for_state(self, state: str):
        """
        Wait for the cluster's instances to a reach a specific state.
        The state of any services installed on the cluster is a
        separate matter.

        This method updates the cluster's instance metadata and
        master and slave IP addresses and hostnames.
        """
        connection = boto.ec2.connect_to_region(region_name=self.region)
        while any([i.state != state for i in self.instances]):
            # Update metadata for all instances in one shot. We don't want
            # to make a call to AWS for each of potentially hundreds of
            # instances.
            instances = connection.get_only_instances(
                instance_ids=[i.id for i in self.instances])
            (self.master_instance, self.slave_instances) = _get_cluster_master_slaves(instances)
            self.master_ip = self.master_instance.ip_address
            self.master_host = self.master_instance.public_dns_name
            self.slave_ips = [i.ip_address for i in self.slave_instances]
            self.slave_hosts = [i.public_dns_name for i in self.slave_instances]
            time.sleep(3)

    def destroy(self):
        self.destroy_check()
        super().destroy()
        connection = boto.ec2.connect_to_region(region_name=self.region)

        # TODO: Centralize logic to get Flintrock base security group. (?)
        flintrock_base_group = connection.get_all_security_groups(
            groupnames=['flintrock'])

        # We "unassign" the cluster security group here (i.e. the
        # 'flintrock-clustername' group) so that we can immediately delete it once
        # the instances are terminated. If we don't do this, we get dependency
        # violations for a couple of minutes before we can actually delete the group.
        # TODO: Is there a way to do this in one call for all instances?
        #       Do we need to throttle these calls?
        for instance in self.instances:
            connection.modify_instance_attribute(
                instance_id=instance.id,
                attribute='groupSet',
                value=flintrock_base_group)
        # TODO: Centralize logic to get cluster security group name from cluster name.
        connection.delete_security_group(name='flintrock-' + self.name)

        connection.terminate_instances(
            instance_ids=[instance.id for instance in self.instances])

    def start_check(self):
        if self.state == 'running':
            raise NothingToDo("Cluster is already running.")
        elif self.state != 'stopped':
            raise ClusterInvalidState(
                attempted_command='start',
                state=self.state)

    @timeit
    def start(self, *, user: str, identity_file: str):
        # TODO: Do these _check() methods make sense here?
        self.start_check()
        connection = boto.ec2.connect_to_region(region_name=self.region)
        connection.start_instances(
            instance_ids=[instance.id for instance in self.instances])
        self.wait_for_state('running')

        super().start(
            user=user,
            identity_file=identity_file)

    def stop_check(self):
        if self.state == 'stopped':
            raise NothingToDo("Cluster is already stopped.")
        elif self.state != 'running':
            raise ClusterInvalidState(
                attempted_command='stop',
                state=self.state)

    @timeit
    def stop(self):
        self.stop_check()
        super().stop()

        connection = boto.ec2.connect_to_region(region_name=self.region)
        connection.stop_instances(
            instance_ids=[instance.id for instance in self.instances])
        self.wait_for_state('stopped')

    def run_command_check(self):
        if self.state != 'running':
            raise ClusterInvalidState(
                attempted_command='run-command',
                state=self.state)

    @timeit
    def run_command(self, *, master_only, command, user, identity_file):
        self.run_command_check()
        super().run_command(
            master_only=master_only,
            user=user,
            identity_file=identity_file,
            command=command)

    def copy_file_check(self):
        if self.state != 'running':
            raise ClusterInvalidState(
                attempted_command='copy-file',
                state=self.state)

    @timeit
    def copy_file(self, *, local_path, remote_path, master_only=False, user, identity_file):
        self.copy_file_check()
        super().copy_file(
            master_only=master_only,
            user=user,
            identity_file=identity_file,
            local_path=local_path,
            remote_path=remote_path)

    def print(self):
        """
        Print information about the cluster to screen in YAML.

        We don't use PyYAML because we want to control the key order
        in the output.
        """
        # Mark the boundaries of the YAML output.
        # See: http://yaml.org/spec/current.html#id2525905
        # print('---')
        print(self.name + ':')
        print('  state: {s}'.format(s=self.state))
        print('  node-count: {nc}'.format(nc=len(self.instances)))
        if self.state == 'running':
            print('  master:', self.master_host)
            print('\n    - '.join(['  slaves:'] + self.slave_hosts))
        # print('...')


def get_or_create_ec2_security_groups(
        *,
        cluster_name,
        vpc_id,
        region) -> 'List[boto.ec2.securitygroup.SecurityGroup]':
    """
    If they do not already exist, create all the security groups needed for a
    Flintrock cluster.
    """
    connection = boto.ec2.connect_to_region(region_name=region)

    SecurityGroupRule = namedtuple(
        'SecurityGroupRule', [
            'ip_protocol',
            'from_port',
            'to_port',
            'src_group',
            'cidr_ip'])

    # TODO: Make these into methods, since we need this logic (though simple)
    #       in multiple places. (?)
    flintrock_group_name = 'flintrock'
    cluster_group_name = 'flintrock-' + cluster_name

    search_results = connection.get_all_security_groups(
        filters={
            'group-name': [flintrock_group_name, cluster_group_name]
        })

    # The Flintrock group is common to all Flintrock clusters and authorizes client traffic
    # to them.
    flintrock_group = next((sg for sg in search_results if sg.name == flintrock_group_name), None)

    # The cluster group is specific to one Flintrock cluster and authorizes intra-cluster
    # communication.
    cluster_group = next((sg for sg in search_results if sg.name == cluster_group_name), None)

    if not flintrock_group:
        flintrock_group = connection.create_security_group(
            name=flintrock_group_name,
            description="Flintrock base group",
            vpc_id=vpc_id)

    # Rules for the client interacting with the cluster.
    flintrock_client_ip = (
        urllib.request.urlopen('http://checkip.amazonaws.com/')
        .read().decode('utf-8').strip())
    flintrock_client_cidr = '{ip}/32'.format(ip=flintrock_client_ip)

    # TODO: Services should be responsible for registering what ports they want exposed.
    client_rules = [
        # SSH
        SecurityGroupRule(
            ip_protocol='tcp',
            from_port=22,
            to_port=22,
            cidr_ip=flintrock_client_cidr,
            src_group=None),
        # HDFS
        SecurityGroupRule(
            ip_protocol='tcp',
            from_port=50070,
            to_port=50070,
            cidr_ip=flintrock_client_cidr,
            src_group=None),
        # Spark
        SecurityGroupRule(
            ip_protocol='tcp',
            from_port=8080,
            to_port=8081,
            cidr_ip=flintrock_client_cidr,
            src_group=None),
        SecurityGroupRule(
            ip_protocol='tcp',
            from_port=4040,
            to_port=4040,
            cidr_ip=flintrock_client_cidr,
            src_group=None)
    ]

    # TODO: Don't try adding rules that already exist.
    # TODO: Add rules in one shot.
    for rule in client_rules:
        try:
            flintrock_group.authorize(**rule._asdict())
        except boto.exception.EC2ResponseError as e:
            if e.error_code != 'InvalidPermission.Duplicate':
                print("Error adding rule: {r}".format(r=rule), file=sys.stderr)
                raise

    # Rules for internal cluster communication.
    if not cluster_group:
        cluster_group = connection.create_security_group(
            name=cluster_group_name,
            description="Flintrock cluster group",
            vpc_id=vpc_id)

    cluster_rules = [
        SecurityGroupRule(
            ip_protocol='icmp',
            from_port=-1,
            to_port=-1,
            src_group=cluster_group,
            cidr_ip=None),
        SecurityGroupRule(
            ip_protocol='tcp',
            from_port=0,
            to_port=65535,
            src_group=cluster_group,
            cidr_ip=None),
        SecurityGroupRule(
            ip_protocol='udp',
            from_port=0,
            to_port=65535,
            src_group=cluster_group,
            cidr_ip=None)
    ]

    # TODO: Don't try adding rules that already exist.
    # TODO: Add rules in one shot.
    for rule in cluster_rules:
        try:
            cluster_group.authorize(**rule._asdict())
        except boto.exception.EC2ResponseError as e:
            if e.error_code != 'InvalidPermission.Duplicate':
                print("Error adding rule: {r}".format(r=rule), file=sys.stderr)
                raise

    return [flintrock_group, cluster_group]


def get_ec2_block_device_map(
        *,
        ami: str,
        region: str) -> boto.ec2.blockdevicemapping.BlockDeviceMapping:
    """
    Get the block device map we should assign to instances launched from a given AMI.

    This is how we configure storage on the instance.
    """
    connection = boto.ec2.connect_to_region(region_name=region)

    image = connection.get_image(ami)
    root_device = boto.ec2.blockdevicemapping.BlockDeviceType(
        # Max root volume size for instance store-backed AMIs is 10 GiB.
        # See: http://docs.aws.amazon.com/AWSEC2/latest/UserGuide/add-instance-store-volumes.html
        size=30 if image.root_device_type == 'ebs' else 10,  # GiB
        volume_type='gp2',  # general-purpose SSD
        delete_on_termination=True)

    block_device_map = boto.ec2.blockdevicemapping.BlockDeviceMapping()
    block_device_map[image.root_device_name] = root_device

    for i in range(12):
        ephemeral_device = boto.ec2.blockdevicemapping.BlockDeviceType(
            ephemeral_name='ephemeral' + str(i))
        ephemeral_device_name = '/dev/sd' + string.ascii_lowercase[i + 1]
        block_device_map[ephemeral_device_name] = ephemeral_device

    return block_device_map


@timeit
def launch(
        *,
        cluster_name,
        num_slaves,
        services,
        assume_yes,
        key_name, identity_file,
        instance_type,
        region,
        availability_zone,
        ami,
        user,
        spot_price=None,
        vpc_id, subnet_id,
        instance_profile_name,
        placement_group,
        tenancy='default',
        ebs_optimized=False,
        instance_initiated_shutdown_behavior='stop'):
    try:
        get_cluster(cluster_name=cluster_name, region=region)
    except ClusterNotFound as e:
        pass
    else:
        raise ClusterAlreadyExists(
            "Cluster {c} already exists in region {r}.".format(
                c=cluster_name,
                r=region))

    try:
        security_groups = get_or_create_ec2_security_groups(
            cluster_name=cluster_name,
            vpc_id=vpc_id,
            region=region)
        block_device_map = get_ec2_block_device_map(
            ami=ami,
            region=region)
    except boto.exception.EC2ResponseError as e:
        if e.error_code == 'InvalidAMIID.NotFound':
            raise Exception(
                "Error: Could not find {ami} in region {region}.".format(
                    ami=ami,
                    region=region))
        else:
            raise

    connection = boto.ec2.connect_to_region(region_name=region)

    num_instances = num_slaves + 1
    spot_requests = []
    cluster_instances = []

    try:
        if spot_price:
            print("Requesting {c} spot instances at a max price of ${p}...".format(
                c=num_instances, p=spot_price))

            spot_requests = connection.request_spot_instances(
                price=spot_price,
                image_id=ami,
                count=num_instances,
                key_name=key_name,
                instance_type=instance_type,
                block_device_map=block_device_map,
                instance_profile_name=instance_profile_name,
                placement=availability_zone,
                security_group_ids=[sg.id for sg in security_groups],
                subnet_id=subnet_id,
                placement_group=placement_group,
                ebs_optimized=ebs_optimized)

            request_ids = [r.id for r in spot_requests]
            pending_request_ids = request_ids

            while pending_request_ids:
                print("{grant} of {req} instances granted. Waiting...".format(
                    grant=num_instances - len(pending_request_ids),
                    req=num_instances))
                time.sleep(30)
                spot_requests = connection.get_all_spot_instance_requests(request_ids=request_ids)
                pending_request_ids = [r.id for r in spot_requests if r.state != 'active']

            print("All {c} instances granted.".format(c=num_instances))

            cluster_instances = connection.get_only_instances(
                instance_ids=[r.instance_id for r in spot_requests])
        else:
            print("Launching {c} instances...".format(c=num_instances))

            reservation = connection.run_instances(
                image_id=ami,
                min_count=num_instances,
                max_count=num_instances,
                key_name=key_name,
                instance_type=instance_type,
                block_device_map=block_device_map,
                placement=availability_zone,
                security_group_ids=[sg.id for sg in security_groups],
                subnet_id=subnet_id,
                instance_profile_name=instance_profile_name,
                placement_group=placement_group,
                tenancy=tenancy,
                ebs_optimized=ebs_optimized,
                instance_initiated_shutdown_behavior=instance_initiated_shutdown_behavior)

            cluster_instances = reservation.instances

            time.sleep(10)  # AWS metadata eventual consistency tax.

        master_instance = cluster_instances[0]
        slave_instances = cluster_instances[1:]

        connection.create_tags(
            resource_ids=[master_instance.id],
            tags={
                'flintrock-role': 'master',
                'Name': '{c}-master'.format(c=cluster_name)})
        connection.create_tags(
            resource_ids=[i.id for i in slave_instances],
            tags={
                'flintrock-role': 'slave',
                'Name': '{c}-slave'.format(c=cluster_name)})

        cluster = EC2Cluster(
            name=cluster_name,
            region=region,
            ssh_key_pair=generate_ssh_key_pair(),
            master_ip=None,
            master_host=None,
            master_instance=master_instance,
            slave_ips=None,
            slave_hosts=None,
            slave_instances=slave_instances)

        cluster.wait_for_state('running')

        provision_cluster(
            cluster=cluster,
            services=services,
            user=user,
            identity_file=identity_file)

    except (Exception, KeyboardInterrupt) as e:
        # TODO: Cleanup cluster security group here.
        print(e, file=sys.stderr)

        if spot_requests:
            # TODO: Do this only if there are pending requests.
            print("Canceling spot instance requests...", file=sys.stderr)
            request_ids = [r.id for r in spot_requests]
            connection.cancel_spot_instance_requests(
                request_ids=request_ids)
            # Make sure we have the latest information on any launched spot instances.
            spot_requests = connection.get_all_spot_instance_requests(
                request_ids=request_ids)
            instance_ids = [r.instance_id for r in spot_requests if r.instance_id]
            if instance_ids:
                cluster_instances = connection.get_only_instances(
                    instance_ids=instance_ids)

        if cluster_instances:
            if not assume_yes:
                yes = click.confirm(
                    text="Do you want to terminate the {c} instances created by this operation?"
                         .format(c=len(cluster_instances)),
                    err=True,
                    default=True)

            if assume_yes or yes:
                print("Terminating instances...", file=sys.stderr)
                connection.terminate_instances(
                    instance_ids=[instance.id for instance in cluster_instances])

        raise


def get_cluster(*, cluster_name: str, region: str) -> EC2Cluster:
    """
    Get an existing EC2 cluster.
    """
    return get_clusters(
        cluster_names=[cluster_name],
        region=region)[0]


def get_clusters(*, cluster_names: list=[], region: str) -> list:
    """
    Get all the named clusters. If no names are given, get all clusters.

    We do a little extra work here so that we only make one call to AWS
    regardless of how many clusters we have to look up. That's because querying
    AWS -- a network operation -- is by far the slowest step.
    """
    connection = boto.ec2.connect_to_region(region_name=region)

    if cluster_names:
        group_name_filter = ['flintrock-' + cn for cn in cluster_names]
    else:
        group_name_filter = 'flintrock'

    all_clusters_instances = connection.get_only_instances(
        filters={
            'instance.group-name': group_name_filter
        })

    found_cluster_names = {
        _get_cluster_name(instance) for instance in all_clusters_instances}

    if cluster_names:
        missing_cluster_names = set(cluster_names) - found_cluster_names
        if missing_cluster_names:
            raise ClusterNotFound("No cluster {c} in region {r}.".format(
                c=missing_cluster_names.pop(),
                r=region))

    clusters = [
        _compose_cluster(
            name=cluster_name,
            region=region,
            instances=list(filter(
                lambda x: _get_cluster_name(x) == cluster_name, all_clusters_instances)))
        for cluster_name in found_cluster_names]

    return clusters


def _get_cluster_name(instance: boto.ec2.instance.Instance) -> str:
    """
    Given an EC2 instance, get the name of the Flintrock cluster it belongs to.
    """
    for group in instance.groups:
        if group.name.startswith('flintrock-'):
            return group.name.replace('flintrock-', '', 1)


def _get_cluster_master_slaves(instances: list) -> (boto.ec2.instance.Instance, list):
    """
    Get the master and slave instances from a set of raw EC2 instances representing
    a Flintrock cluster.
    """
    # TODO: Raise clean errors if a cluster is malformed somehow.
    #       e.g. No master, multiple masters, no slaves, etc.
    master_instance = list(filter(
        lambda x: x.tags['flintrock-role'] == 'master',
        instances))[0]
    slave_instances = list(filter(
        lambda x: x.tags['flintrock-role'] == 'slave',
        instances))
    return (master_instance, slave_instances)


def _compose_cluster(*, name: str, region: str, instances: list) -> EC2Cluster:
    """
    Compose an EC2Cluster object from a set of raw EC2 instances representing
    a Flintrock cluster.
    """
    (master_instance, slave_instances) = _get_cluster_master_slaves(instances)

    cluster = EC2Cluster(
        name=name,
        master_ip=master_instance.ip_address,
        master_host=master_instance.public_dns_name,
        slave_ips=[i.ip_address for i in slave_instances],
        slave_hosts=[i.public_dns_name for i in slave_instances],
        region=region,
        master_instance=master_instance,
        slave_instances=slave_instances)

    return cluster