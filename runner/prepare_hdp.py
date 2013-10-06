#!/usr/bin/env python
# -*- coding: utf-8 -*-

#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from __future__ import with_statement

import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib2
from optparse import OptionParser
from sys import stderr
import boto
from boto.ec2.blockdevicemapping import BlockDeviceMapping, EBSBlockDeviceType
from boto import ec2

# Configure and parse our command-line arguments
def parse_args():
  parser = OptionParser(usage="spark-ec2 [options] <action> <cluster_name>"
      + "\n\n<action> can be: launch, destroy, login, stop, start, get-master",
      add_help_option=False)
  parser.add_option("-h", "--help", action="help",
                    help="Show this help message and exit")
  parser.add_option("-s", "--slaves", type="int", default=1,
      help="Number of slaves to launch (default: 1)")
  parser.add_option("-w", "--wait", type="int", default=120,
      help="Seconds to wait for nodes to start (default: 120)")
  parser.add_option("-k", "--key-pair",
      help="Key pair to use on instances")
  parser.add_option("-i", "--identity-file",
      help="SSH private key file to use for logging into instances")
  parser.add_option("-t", "--instance-type", default="m1.large",
      help="Type of instance to launch (default: m1.large). " +
           "WARNING: must be 64-bit; small instances won't work")
  parser.add_option("-m", "--master-instance-type", default="",
      help="Master instance type (leave empty for same as instance-type)")
  parser.add_option("-r", "--region", default="us-east-1",
      help="EC2 region zone to launch instances in")
  parser.add_option("-z", "--zone", default="",
      help="Availability zone to launch instances in, or 'all' to spread " +
           "slaves across multiple (an additional $0.01/Gb for bandwidth" +
           "between zones applies)")
  parser.add_option("-a", "--ami", help="Amazon Machine Image ID to use",
                    default="ami-a25415cb")
  parser.add_option("-v", "--spark-version", default="0.8.0",
      help="Version of Spark to use: 'X.Y.Z' or a specific git hash")
  parser.add_option("--spark-git-repo", 
      default="https://github.com/mesos/spark", 
      help="Github repo from which to checkout supplied commit hash")
  parser.add_option("--hadoop-major-version", default="1",
      help="Major version of Hadoop (default: 1)")
  parser.add_option("-D", metavar="[ADDRESS:]PORT", dest="proxy_port", 
      help="Use SSH dynamic port forwarding to create a SOCKS proxy at " +
            "the given local address (for use with login)")
  parser.add_option("--resume", action="store_true", default=False,
      help="Resume installation on a previously launched cluster " +
           "(for debugging)")
  parser.add_option("--ebs-vol-size", metavar="SIZE", type="int", default=0,
      help="Attach a new EBS volume of size SIZE (in GB) to each node as " +
           "/vol. The volumes will be deleted when the instances terminate. " +
           "Only possible on EBS-backed AMIs.")
  parser.add_option("--swap", metavar="SWAP", type="int", default=1024,
      help="Swap space to set up per node, in MB (default: 1024)")
  parser.add_option("--spot-price", metavar="PRICE", type="float",
      help="If specified, launch slaves as spot instances with the given " +
            "maximum price (in dollars)")
  parser.add_option("--ganglia", action="store_true", default=True,
      help="Setup Ganglia monitoring on cluster (default: on). NOTE: " +
           "the Ganglia page will be publicly accessible")
  parser.add_option("--no-ganglia", action="store_false", dest="ganglia",
      help="Disable Ganglia monitoring for the cluster")
  parser.add_option("-u", "--user", default="root",
      help="The SSH user you want to connect as (default: root)")
  parser.add_option("--delete-groups", action="store_true", default=False,
      help="When destroying a cluster, delete the security groups that were created")

  (opts, args) = parser.parse_args()
  if len(args) != 2:
    parser.print_help()
    sys.exit(1)
  (action, cluster_name) = args
  if opts.identity_file == None and action in ['launch', 'login', 'start']:
    print >> stderr, ("ERROR: The -i or --identity-file argument is " +
                      "required for " + action)
    sys.exit(1)
  
  # Boto config check
  # http://boto.cloudhackers.com/en/latest/boto_config_tut.html
  home_dir = os.getenv('HOME')
  if home_dir == None or not os.path.isfile(home_dir + '/.boto'):
    if not os.path.isfile('/etc/boto.cfg'):
      if os.getenv('AWS_ACCESS_KEY_ID') == None:
        print >> stderr, ("ERROR: The environment variable AWS_ACCESS_KEY_ID " +
                          "must be set")
        sys.exit(1)
      if os.getenv('AWS_SECRET_ACCESS_KEY') == None:
        print >> stderr, ("ERROR: The environment variable AWS_SECRET_ACCESS_KEY " +
                          "must be set")
        sys.exit(1)
  return (opts, action, cluster_name)


# Get the EC2 security group of the given name, creating it if it doesn't exist
def get_or_make_group(conn, name):
  groups = conn.get_all_security_groups()
  group = [g for g in groups if g.name == name]
  if len(group) > 0:
    return group[0]
  else:
    print "Creating security group " + name
    return conn.create_security_group(name, "Spark EC2 group")


# Wait for a set of launched instances to exit the "pending" state
# (i.e. either to start running or to fail and be terminated)
def wait_for_instances(conn, instances):
  while True:
    for i in instances:
      i.update()
    if len([i for i in instances if i.state == 'pending']) > 0:
      time.sleep(5)
    else:
      return


# Check whether a given EC2 instance object is in a state we consider active,
# i.e. not terminating or terminated. We count both stopping and stopped as
# active since we can restart stopped clusters.
def is_active(instance):
  return (instance.state in ['pending', 'running', 'stopping', 'stopped'])

# Launch a cluster of the given name, by setting up its security groups,
# and then starting new instances in them.
# Returns a tuple of EC2 reservation objects for the master and slaves
# Fails if there already instances running in the cluster's groups.
def launch_cluster(conn, opts, cluster_name):
  print "Setting up security groups..."
  master_group = get_or_make_group(conn, cluster_name + "-master")
  slave_group = get_or_make_group(conn, cluster_name + "-slaves")
  ambari_group = get_or_make_group(conn, cluster_name + "-ambari")

  if master_group.rules == []: # Group was just now created
    master_group.authorize(src_group=master_group)
    master_group.authorize(src_group=slave_group)
    master_group.authorize(src_group=ambari_group)
    # TODO: Currently Group is completely open
    master_group.authorize('tcp', 0, 65535, '0.0.0.0/0')
  if slave_group.rules == []: # Group was just now created
    slave_group.authorize(src_group=master_group)
    slave_group.authorize(src_group=slave_group)
    slave_group.authorize(src_group=ambari_group)
    # TODO: Currently Group is completely open
    slave_group.authorize('tcp', 0, 65535, '0.0.0.0/0')
  if ambari_group.rules == []: # Group was just now created
    ambari_group.authorize(src_group=master_group)
    ambari_group.authorize(src_group=slave_group)
    ambari_group.authorize(src_group=ambari_group)
    # TODO: Currently Group is completely open
    ambari_group.authorize('tcp', 0, 65535, '0.0.0.0/0')

  # Check if instances are already running in our groups
  if opts.resume:
    return get_existing_cluster(conn, opts, cluster_name, die_on_error=False)
  else:
    active_nodes = get_existing_cluster(conn, opts, cluster_name, die_on_error=False)
    if any(active_nodes):
      print >> stderr, ("ERROR: There are already instances running in " +
          "group %s or %s" % (master_group.name, slave_group.name))
      sys.exit(1)

    print "Launching instances..."

    try:
      image = conn.get_all_images(image_ids=[opts.ami])[0]
    except:
      print >> stderr, "Could not find AMI " + opts.ami
      sys.exit(1)

    # Create block device mapping so that we can add an EBS volume if asked to
    block_map = BlockDeviceMapping()
    if opts.ebs_vol_size > 0:
      device = EBSBlockDeviceType()
      device.size = opts.ebs_vol_size
      device.delete_on_termination = True
      block_map["/dev/sdv"] = device

    # Launch slaves
    # Launch non-spot instances
    zones = get_zones(conn, opts)
    num_zones = len(zones)
    i = 0
    slave_nodes = []
    for zone in zones:
      num_slaves_this_zone = get_partition(opts.slaves, num_zones, i)
      if num_slaves_this_zone > 0:
        slave_res = image.run(key_name = opts.key_pair,
                              security_groups = [slave_group],
                              instance_type = opts.instance_type,
                              placement = zone,
                              min_count = num_slaves_this_zone,
                              max_count = num_slaves_this_zone,
                              block_device_map = block_map)
        slave_nodes += slave_res.instances
        print "Launched %d slaves in %s, regid = %s" % (num_slaves_this_zone,
                                                        zone, slave_res.id)
      i += 1

    # Launch masters
    master_type = opts.master_instance_type
    if master_type == "":
      master_type = opts.instance_type
    if opts.zone == 'all':
      opts.zone = random.choice(conn.get_all_zones()).name
    master_res = image.run(key_name = opts.key_pair,
                          security_groups = [master_group],
                          instance_type = master_type,
                          placement = opts.zone,
                          min_count = 1,
                          max_count = 1,
                          block_device_map = block_map)
    master_nodes = master_res.instances
    print "Launched master in %s, regid = %s" % (zone, master_res.id)

    ambari_type = opts.master_instance_type
    if ambari_type == "":
      ambari_type = opts.instance_type
    if opts.zone == 'all':
      opts.zone = random.choice(conn.get_all_zones()).name
    ambari_res = image.run(key_name = opts.key_pair,
                          security_groups = [ambari_group],
                          instance_type = ambari_type,
                          placement = opts.zone,
                          min_count = 1,
                          max_count = 1,
                          block_device_map = block_map)
    ambari_nodes = ambari_res.instances
    print "Launched ambari in %s, regid = %s" % (zone, ambari_res.id)

    # Return all the instances
    return (master_nodes, slave_nodes, ambari_nodes)


# Get the EC2 instances in an existing cluster if available.
# Returns a tuple of lists of EC2 instance objects for the masters and slaves
def get_existing_cluster(conn, opts, cluster_name, die_on_error=True):
  print "Searching for existing cluster " + cluster_name + "..."
  reservations = conn.get_all_instances()
  master_nodes = []
  slave_nodes = []
  ambari_nodes = []
  for res in reservations:
    active = [i for i in res.instances if is_active(i)]
    if len(active) > 0:
      group_names = [g.name for g in res.groups]
      if group_names == [cluster_name + "-master"]:
        master_nodes += res.instances
      elif group_names == [cluster_name + "-slaves"]:
        slave_nodes += res.instances
      elif group_names == [cluster_name + "-ambari"]:
        ambari_nodes += res.instances
  if any((master_nodes, slave_nodes, ambari_nodes)):
    print ("Found %d master(s), %d slaves, %d ambari" %
           (len(master_nodes), len(slave_nodes), len(ambari_nodes)))
  if (master_nodes != [] and slave_nodes != [] and ambari_nodes != []) or not die_on_error:
    return (master_nodes, slave_nodes, ambari_nodes)
  else:
    print "ERROR: Could not find any existing cluster"
    sys.exit(1)


# Deploy configuration files and run setup scripts on a newly launched
# or started EC2 cluster.
def setup_cluster(conn, master_nodes, slave_nodes, ambari_nodes, opts, deploy_ssh_key):
  master = master_nodes[0]
  ambari = ambari_nodes[0]

  print "Ambari: %s" % ambari.public_dns_name

  opts.user = "ec2-user"

  if deploy_ssh_key:
    print "Copying SSH key %s to master..." % opts.identity_file
    ssh(master.public_dns_name, opts, 'mkdir -p ~/.ssh')
    scp(master.public_dns_name, opts, opts.identity_file, '~/.ssh/id_rsa')
    ssh(master.public_dns_name, opts, 'chmod 600 ~/.ssh/id_rsa')

    print "Copying SSH key %s to ambari..." % opts.identity_file
    ssh(ambari.public_dns_name, opts, 'mkdir -p ~/.ssh')
    scp(ambari.public_dns_name, opts, opts.identity_file, '~/.ssh/id_rsa')
    ssh(ambari.public_dns_name, opts, 'chmod 600 ~/.ssh/id_rsa')

  for node in master_nodes + slave_nodes + ambari_nodes:
    ssh(node.public_dns_name, opts, 'echo "PermitRootLogin yes"|sudo tee -a /etc/ssh/sshd_config')
    ssh(node.public_dns_name, opts, 'sudo cp /home/ec2-user/.ssh/authorized_keys /root/.ssh/authorized_keys; sudo /etc/init.d/sshd restart;')

  opts.user = "root"

  configure_node(master, opts, "hdpmaster1")
  configure_node(ambari, opts, "ambarimaster")
  for i, node in enumerate(slave_nodes):
    configure_node(node, opts, "hdpslave%i" % i)

  wait_for_cluster(conn, 90, master_nodes, slave_nodes, ambari_nodes)

  setup_ambari_master(ambari, opts)
  generate_hosts_and_key(master_nodes + ambari_nodes + slave_nodes, opts)

  modules = ['spark', 'shark', 'ephemeral-hdfs', 'persistent-hdfs', 
             'mapreduce', 'spark-standalone']

  if opts.hadoop_major_version == "1":
    modules = filter(lambda x: x != "mapreduce", modules)

  if opts.ganglia:
    modules.append('ganglia')

  # NOTE: We should clone the repository before running deploy_files to
  # prevent ec2-variables.sh from being overwritten
  ssh(master, opts, "rm -rf spark-ec2 && git clone https://github.com/mesos/spark-ec2.git -b v2")

  print "Deploying files to master..."
  deploy_files(conn, "deploy.generic", opts, master_nodes, slave_nodes, modules)

  print "Running setup on master..."
  setup_spark_cluster(master, opts)
  print "Done!"


def configure_node(node, opts, name):
  cmd = """
        sed -e 's/SELINUX=enforcing/SELINUX=disabled/g' /etc/sysconfig/selinux > /etc/sysconfig/selinux;
        sed -e 's/HOSTNAME.\+/%s.hdp.hadoop/g' /etc/sysconfig/network > /etc/sysconfig/network;
        chkconfig iptables off;
        chkconfig ip6tables off;
        shutdown -r now;
        """ % name

  node.assigned_name = name
  ssh(node.public_dns_name, opts, cmd)

def generate_hosts_and_key(nodes, opts):
  tmp_hosts_file = tempfile.NamedTemporaryFile(delete=False)
  print >> tmp_hosts_file, "127.0.0.1 localhost.localdomain localhost"
  print >> tmp_hosts_file, "::1 localhost6.localdomain6 localhost6"

  for node in nodes:
    print >> tmp_hosts_file, "%s %s.hdp.hadoop %s" % (node.ip_address, node.assigned_name, node.assigned_name)
  tmp_hosts_file.close()

  print open(tmp_hosts_file.name).readlines()
  for node in nodes:
    scp(node.public_dns_name, opts, tmp_hosts_file.name, "/etc/hosts")
    scp(node.public_dns_name, opts, "ambari.pub", "/root/.ssh/ambari.pub")
    ssh(node.public_dns_name, opts, "cat /root/.ssh/ambari.pub >> /root/.ssh/authorized_keys")
    ssh(node.public_dns_name, opts, "hostname %s.hdp.hadoop" % node.assigned_name)
    ssh(node.public_dns_name, opts, "/etc/init.d/ntpd restart")

def setup_ambari_master(ambari, opts):
  cmd = """
        wget http://public-repo-1.hortonworks.com/ambari/centos6/1.x/GA/ambari.repo;
        cp ambari.repo /etc/yum.repos.d;
        yum -y install epel-release;
        yum -y repolist;
        yum -y install ambari-server;
        ambari-server setup;
        ambari-server start;
        ambari-server status;
        ssh-keygen -t rsa;
        """
  cmd = cmd.replace('\n', ' ')
  ssh(ambari.public_dns_name, opts, cmd)
  scp_download(ambari.public_dns_name, opts, "/root/.ssh/id_rsa.pub", "ambari.pub")
  scp_download(ambari.public_dns_name, opts, "/root/.ssh/id_rsa", "ambari")

def setup_spark_cluster(master, opts):
  ssh(master, opts, "chmod u+x spark-ec2/setup.sh")
  ssh(master, opts, "spark-ec2/setup.sh")
  print "Spark standalone cluster started at http://%s:8080" % master

  if opts.ganglia:
    print "Ganglia started at http://%s:5080/ganglia" % master


# Wait for a whole cluster (masters, slaves and ZooKeeper) to start up
def wait_for_cluster(conn, wait_secs, master_nodes, slave_nodes, ambari_nodes):
  print "Waiting for instances to start up..."
  time.sleep(5)
  wait_for_instances(conn, master_nodes)
  wait_for_instances(conn, slave_nodes)
  wait_for_instances(conn, ambari_nodes)
  print "Waiting %d more seconds..." % wait_secs
  time.sleep(wait_secs)


# Get number of local disks available for a given EC2 instance type.
def get_num_disks(instance_type):
  # From http://docs.amazonwebservices.com/AWSEC2/latest/UserGuide/index.html?InstanceStorage.html
  disks_by_instance = {
    "m1.small":    1,
    "m1.medium":   1,
    "m1.large":    2,
    "m1.xlarge":   4,
    "t1.micro":    1,
    "c1.medium":   1,
    "c1.xlarge":   4,
    "m2.xlarge":   1,
    "m2.2xlarge":  1,
    "m2.4xlarge":  2,
    "cc1.4xlarge": 2,
    "cc2.8xlarge": 4,
    "cg1.4xlarge": 2,
    "hs1.8xlarge": 24,
    "cr1.8xlarge": 2,
    "hi1.4xlarge": 2,
    "m3.xlarge":   0,
    "m3.2xlarge":  0
  }
  if instance_type in disks_by_instance:
    return disks_by_instance[instance_type]
  else:
    print >> stderr, ("WARNING: Don't know number of disks on instance type %s; assuming 1"
                      % instance_type)
    return 1


# Deploy the configuration file templates in a given local directory to
# a cluster, filling in any template parameters with information about the
# cluster (e.g. lists of masters and slaves). Files are only deployed to
# the first master instance in the cluster, and we expect the setup
# script to be run on that instance to copy them to other nodes.
def deploy_files(conn, root_dir, opts, master_nodes, slave_nodes, modules):
  active_master = master_nodes[0].public_dns_name

  num_disks = get_num_disks(opts.instance_type)
  hdfs_data_dirs = "/mnt/ephemeral-hdfs/data"
  mapred_local_dirs = "/mnt/hadoop/mrlocal"
  spark_local_dirs = "/mnt/spark"
  if num_disks > 1:
    for i in range(2, num_disks + 1):
      hdfs_data_dirs += ",/mnt%d/ephemeral-hdfs/data" % i
      mapred_local_dirs += ",/mnt%d/hadoop/mrlocal" % i
      spark_local_dirs += ",/mnt%d/spark" % i

  cluster_url = "%s:7077" % active_master

  if "." in opts.spark_version:
    # Pre-built spark & shark deploy
    (spark_v, shark_v) = get_spark_shark_version(opts)
  else:
    # Spark-only custom deploy
    spark_v = "%s|%s" % (opts.spark_git_repo, opts.spark_version)
    shark_v = ""
    modules = filter(lambda x: x != "shark", modules)

  template_vars = {
    "master_list": '\n'.join([i.public_dns_name for i in master_nodes]),
    "active_master": active_master,
    "slave_list": '\n'.join([i.public_dns_name for i in slave_nodes]),
    "cluster_url": cluster_url,
    "hdfs_data_dirs": hdfs_data_dirs,
    "mapred_local_dirs": mapred_local_dirs,
    "spark_local_dirs": spark_local_dirs,
    "swap": str(opts.swap),
    "modules": '\n'.join(modules),
    "spark_version": spark_v,
    "shark_version": shark_v,
    "hadoop_major_version": opts.hadoop_major_version
  }

  # Create a temp directory in which we will place all the files to be
  # deployed after we substitue template parameters in them
  tmp_dir = tempfile.mkdtemp()
  for path, dirs, files in os.walk(root_dir):
    if path.find(".svn") == -1:
      dest_dir = os.path.join('/', path[len(root_dir):])
      local_dir = tmp_dir + dest_dir
      if not os.path.exists(local_dir):
        os.makedirs(local_dir)
      for filename in files:
        if filename[0] not in '#.~' and filename[-1] != '~':
          dest_file = os.path.join(dest_dir, filename)
          local_file = tmp_dir + dest_file
          with open(os.path.join(path, filename)) as src:
            with open(local_file, "w") as dest:
              text = src.read()
              for key in template_vars:
                text = text.replace("{{" + key + "}}", template_vars[key])
              dest.write(text)
              dest.close()
  # rsync the whole directory over to the master machine
  command = (("rsync -rv -e 'ssh -o StrictHostKeyChecking=no -i %s' " +
      "'%s/' '%s@%s:/'") % (opts.identity_file, tmp_dir, opts.user, active_master))
  subprocess.check_call(command, shell=True)
  # Remove the temp directory we created above
  shutil.rmtree(tmp_dir)


# Copy a file to a given host through scp, throwing an exception if scp fails
def scp(host, opts, local_file, dest_file):
  subprocess.check_call(
      "scp -q -o StrictHostKeyChecking=no -i %s '%s' '%s@%s:%s'" %
      (opts.identity_file, local_file, opts.user, host, dest_file), shell=True)

def scp_download(host, opts, remote_file, local_file):
  subprocess.check_call(
      "scp -q -o StrictHostKeyChecking=no -i %s '%s@%s:%s' '%s'" %
      (opts.identity_file, opts.user, host, remote_file, local_file), shell=True)

# Run a command on a host through ssh, retrying up to two times
# and then throwing an exception if ssh continues to fail.
def ssh(host, opts, command):
  cmd = "ssh -t -o StrictHostKeyChecking=no -i %s %s@%s '%s'" % (opts.identity_file, opts.user, host, command)
  print cmd
  tries = 0
  while True:
    try:
      return subprocess.check_call(
        cmd, shell=True)
    except subprocess.CalledProcessError as e:
      if (tries > 2):
        raise e
      print "Couldn't connect to host {0}, waiting 30 seconds".format(e)
      time.sleep(30)
      tries = tries + 1





# Gets a list of zones to launch instances in
def get_zones(conn, opts):
  if opts.zone == 'all':
    zones = [z.name for z in conn.get_all_zones()]
  else:
    zones = [opts.zone]
  return zones


# Gets the number of items in a partition
def get_partition(total, num_partitions, current_partitions):
  num_slaves_this_zone = total / num_partitions
  if (total % num_partitions) - current_partitions > 0:
    num_slaves_this_zone += 1
  return num_slaves_this_zone


def main():
  (opts, action, cluster_name) = parse_args()
  try:
    conn = ec2.connect_to_region(opts.region)
  except Exception as e:
    print >> stderr, (e)
    sys.exit(1)

  # Select an AZ at random if it was not specified.
  if opts.zone == "":
    opts.zone = random.choice(conn.get_all_zones()).name

  (master_nodes, slave_nodes, ambari_nodes) = launch_cluster(conn, opts, cluster_name)
  wait_for_cluster(conn, opts.wait, master_nodes, slave_nodes, ambari_nodes)
  setup_cluster(conn, master_nodes, slave_nodes, ambari_nodes, opts, True)

if __name__ == "__main__":
  logging.basicConfig()
  main()

