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
def setup_cluster(conn, master_nodes, slave_nodes, ambari_nodes, opts, deploy_ssh_key, cluster_name):
  master = master_nodes[0]
  ambari = ambari_nodes[0]

  opts.user = "ec2-user"

  for node in master_nodes + slave_nodes + ambari_nodes:
    ssh(node.public_dns_name, opts, 'echo "PermitRootLogin yes"| sudo tee -a /etc/ssh/sshd_config')
    ssh(node.public_dns_name, opts, 'echo "JAVA_HOME=/usr/local"| sudo tee -a /root/.bash_profile')
    ssh(node.public_dns_name, opts, 'echo "SCALA_HOME=/usr/local"| sudo tee -a /root/.bash_profile')
    ssh(node.public_dns_name, opts, 'sudo cp /home/ec2-user/.ssh/authorized_keys /root/.ssh/authorized_keys; sudo /etc/init.d/sshd restart;')

  opts.user = "root"

  if deploy_ssh_key:
    print "Copying SSH key %s to master..." % opts.identity_file
    ssh(master.public_dns_name, opts, 'mkdir -p ~/.ssh')
    scp(master.public_dns_name, opts, opts.identity_file, '~/.ssh/id_rsa')
    ssh(master.public_dns_name, opts, 'chmod 600 ~/.ssh/id_rsa')

    print "Copying SSH key %s to ambari..." % opts.identity_file
    ssh(ambari.public_dns_name, opts, 'mkdir -p ~/.ssh')
    scp(ambari.public_dns_name, opts, opts.identity_file, '~/.ssh/id_rsa')
    ssh(ambari.public_dns_name, opts, 'chmod 600 ~/.ssh/id_rsa')

  configure_node(master, opts, "hdpmaster1")
  configure_node(ambari, opts, "ambarimaster")
  for i, node in enumerate(slave_nodes):
    configure_node(node, opts, "hdpslave%i" % i)

  wait_for_cluster(conn, 90, master_nodes, slave_nodes, ambari_nodes)

  setup_ambari_master(ambari, opts)

  start_services(master_nodes + ambari_nodes + slave_nodes, opts)

  args = {
    'runner' : '/Users/ahirreddy/Work/benchmark/spark-0.8.0-incubating/ec2/spark-ec2',
    'keyname' : opts.key_pair,
    'idfile' : opts.identity_file,
    'cluster' : cluster_name,
  }

  subprocess.check_call("%(runner)s -k %(keyname)s -i %(idfile)s -w 0 --no-ganglia start %(cluster)s" % args, shell=True)

  print "Ambari: %s" % ambari.public_dns_name
  print "Master: %s" % master.public_dns_name
  for slave in slave_nodes:
    print "Slave: %s" % slave.public_dns_name

  print "Master: %s" % master.private_dns_name
  print "Slaves:"
  for slave in slave_nodes:
    print slave.private_dns_name

def configure_node(node, opts, name):
  cmd = """
        yum -y install git;
        sed -e 's/SELINUX=enforcing//g' /etc/selinux/config > /etc/selinux/config;
        echo "SELINUX=disabled" >> /etc/selinux/config;
        sed -e 's/HOSTNAME.\+/%s.hdp.hadoop/g' /etc/sysconfig/network > /etc/sysconfig/network;
        chkconfig iptables off;
        chkconfig ip6tables off;
        shutdown -r now;
        """ % name

  cmd = cmd.replace('\n', ' ')
  node.assigned_name = name
  ssh(node.public_dns_name, opts, cmd)

def start_services(nodes, opts):
  for node in nodes:
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


# Wait for a whole cluster (masters, slaves and ZooKeeper) to start up
def wait_for_cluster(conn, wait_secs, master_nodes, slave_nodes, ambari_nodes):
  print "Waiting for instances to start up..."
  time.sleep(5)
  wait_for_instances(conn, master_nodes)
  wait_for_instances(conn, slave_nodes)
  wait_for_instances(conn, ambari_nodes)
  print "Waiting %d more seconds..." % wait_secs
  time.sleep(wait_secs)


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
  setup_cluster(conn, master_nodes, slave_nodes, ambari_nodes, opts, True, cluster_name)

if __name__ == "__main__":
  logging.basicConfig()
  main()

