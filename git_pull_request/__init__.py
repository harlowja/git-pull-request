# -*- encoding: utf-8 -*-
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import netrc
import os
import subprocess
import sys
import tempfile

import daiquiri
import github
from six.moves.urllib import parse as urlparse


LOG = daiquiri.getLogger("git-pull-request")


def _run_shell_command(cmd, output=None, raise_on_error=True):
    if output is True:
        output = subprocess.PIPE

    LOG.debug("running %s", cmd)
    cmd = subprocess.Popen(cmd, stdout=output, stderr=output)
    out = cmd.communicate()
    if raise_on_error and cmd.returncode:
        raise RuntimeError("%s returned %d" % (cmd, cmd.returncode))

    if out[0]:
        return out[0].decode()


def get_login_password(site_name="github.com", netrc_file="~/.netrc"):
    """Read a .netrc file and return login/password for LWN."""
    n = netrc.netrc(os.path.expanduser(netrc_file))
    return n.hosts[site_name][0], n.hosts[site_name][2]


def git_remote_matching_url(url):
    remotes = _run_shell_command(["git", "remote", "-v"],
                                 output=True).split('\n')
    for remote in remotes:
        if url + " (push)" in remote:
            return remote.partition("\t")[0]


def git_remote_url(remote="origin"):
    return _run_shell_command(
        ["git", "config", "--get", "remote." + remote + ".url"],
        output=True).strip()


def git_get_branch_name():
    branch = _run_shell_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        output=True).strip()
    if branch == "HEAD":
        raise RuntimeError("Unable to determine current branch")
    return branch


def git_get_remote_for_branch(branch):
    return _run_shell_command(
        ["git", "config", "--get", "branch." + branch + ".remote"],
        output=True, raise_on_error=False).strip()


def git_get_remote_branch_for_branch(branch):
    return _run_shell_command(
        ["git", "config", "--get", "branch." + branch + ".merge"],
        output=True, raise_on_error=False).strip()


def get_github_user_repo_from_url(url):
    parsed = urlparse.urlparse(url)
    if parsed.netloc != "github.com":
        raise ValueError("The remote URL does not point to GitHub: `%s'" % url)
    try:
        user, repo = parsed.path[1:].split("/", 2)
    except (ValueError, IndexError):
        raise RuntimeError("Unable to parse GitHub repository: `%s'" % url)
    return user, repo[:-4]


try:
    user, password = get_login_password()
except KeyError:
    raise RuntimeError(
        "Unable to find your GitHub crediential.\n"
        "Make sure you have a line like this in your ~/.netrc file:\n"
        "machine github.com login <login> password <pwd>"
    )


def main():
    branch = git_get_branch_name()
    if not branch:
        LOG.critical("Unable to find current branch")
        return 10

    LOG.debug("Local branch name is `%s'", branch)

    remote = git_get_remote_for_branch(branch)
    if not remote:
        LOG.debug(
            "Unable to find remote for local branch `%s', using master",
            branch)
        remote = git_get_remote_for_branch("master")
        if not remote:
            LOG.critical(
                "Unable to find remote for local branch `master'",
                branch)
            return 20

    LOG.debug("Remote name for branch `%s' is `%s'", branch, remote)

    remote_url = git_remote_url(remote)
    if not remote_url:
        LOG.critical("Unable to find remote URL for remote `%s'", remote)
        return 30

    LOG.debug("Remote URL for remote `%s' is `%s'", remote, remote_url)

    user_to_fork, reponame_to_fork = get_github_user_repo_from_url(remote_url)
    LOG.debug("GitHub user and repository to fork: %s/%s",
              user_to_fork, reponame_to_fork)

    g = github.Github(user, password)
    g_user = g.get_user()
    repo_to_fork = g.get_user(user_to_fork).get_repo(reponame_to_fork)
    repo_forked = g_user.create_fork(repo_to_fork)
    LOG.info("Forked repository: %s", repo_forked.html_url)

    remote_to_push = git_remote_matching_url(repo_forked.clone_url)

    if remote_to_push:
        LOG.debug("Found forked repository already in remote as `%s'",
                  remote_to_push)
    else:
        # TODO(jd) this name could be an option
        remote_to_push = "github"
        _run_shell_command(
            ["git", "remote", "add", remote_to_push, repo_forked.clone_url])
        LOG.info("Added forked repository as remote `%s'", remote_to_push)

    LOG.info("Force-pushing branch `%s' to remote `%s'",
             branch, remote_to_push)

    _run_shell_command(["git", "push", "-f", remote_to_push, branch])

    pulls = list(repo_to_fork.get_pulls(head=user + ":" + branch))
    if pulls:
        for pull in pulls:
            LOG.info("Pull-request already exists at: " + pull.html_url)
    else:
        # TODO(jd): allow to specify remote branch
        remote_branch = git_get_remote_branch_for_branch(branch) or "master"
        # Create a pull request
        editor = os.getenv("EDITOR")
        if not editor:
            LOG.warning(
                "$EDITOR is unset, you will not be able to edit the "
                "pull-request message")
            editor = "cat"
        fd, bodyfilename = tempfile.mkstemp()
        title = "Merge request for " + branch
        with open(bodyfilename, "w") as body:
            body.write(title + "\n\n")
            body.write(_run_shell_command(
                ["git", "log",
                 "--format=%s",
                 remote + "/" + remote_branch + ".." + branch],
                output=True))
        os.system(editor + " " + bodyfilename)
        with open(bodyfilename, "r") as body:
            content = body.read().strip()
        os.unlink(bodyfilename)

        content_by_line = content.split("\n")
        if len(content_by_line) > 0:
            title = content_by_line[0]
            body = "\n".join(content_by_line[1:])
        else:
            LOG.warning("Pull-request message is empty, aborting")
            return 40

        pull = repo_to_fork.create_pull(base=remote_branch,
                                        head=user + ":" + branch,
                                        title=title,
                                        body=body)
        LOG.info("Pull-request created: " + pull.html_url)


if __name__ == '__main__':

    daiquiri.setup(
        outputs=(
            daiquiri.output.Stream(
                sys.stdout,
                formatter=logging.Formatter(
                    fmt="%(message)s")),),
        level=logging.INFO,
    )

    sys.exit(main())