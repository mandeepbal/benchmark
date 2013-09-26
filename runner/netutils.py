from os.path import abspath, expanduser
from plumbum import SshMachine

# Run a command on a host through ssh, throwing an exception if ssh fails

class SSH:
    def __init__(self, host, username, identity_file, attempts=1):
        identity_file = abspath(expanduser(identity_file))
        self.machine = SshMachine(host, user=username, keyfile=identity_file,
                                  ssh_opts=["-o StrictHostKeyChecking no",
                                            "-t", "-t"])
        self.attempts = attempts

    # Issue a command by directly calling the created object
    def __call__(self, command):
        # Split up the command to make it usable with a SshMachine
        command = command.split(' ')
        executable = command[0]
        args = command[1:]

        for x in xrange(self.attempts-1):
            self.machine[executable](args)

        return self.machine[executable](args)

    # Copy a file to a given host through scp, throwing an exception if scp fails
    def scp_to(self, local_file, remote_file):
        return self.machine.upload(local_file, remote_file)

    # Copy a file to a given host through scp, throwing an exception if scp fails
    def scp_from(self, remote_file, local_file):
        return self.machine.download(remote_file, local_file)

