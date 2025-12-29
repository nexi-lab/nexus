"""
E2B Template for Nexus Integration with FUSE support.
Extends the official code-interpreter-v1 template with additional packages.
"""

from e2b import Template


def make_template() -> Template:
    """
    Create a Nexus-enabled sandbox template that extends code-interpreter-v1.

    This template:
    1. Starts from the official code-interpreter-v1 template
    2. Adds FUSE support for filesystem mounting
    3. Installs Nexus AI filesystem package
    4. Configures passwordless sudo for FUSE operations
    5. Sets up mount points
    6. Inherits the Jupyter server on port 49999 from base template
    """

    template = (
        Template()
        # Start from the official code-interpreter template
        .from_template("code-interpreter-v1")
        # Switch to root for system package installation
        .set_user("root")
        # Install FUSE support and dependencies
        .run_cmd(
            "apt-get update && "
            "apt-get install -y "
            "fuse libfuse2 libfuse-dev pkg-config sudo git curl && "
            "rm -rf /var/lib/apt/lists/*"
        )
        # Install fusepy and Nexus
        .run_cmd(
            "python3 -m pip install --no-cache-dir "
            "'fusepy @ git+https://github.com/fusepy/fusepy.git' "
            "'nexus-ai-fs @ git+https://github.com/nexi-lab/nexus.git@main'"
        )
        # Verify installations
        .run_cmd(
            "python3 -c \"import fuse; print('fusepy OK')\" && "
            "nexus --version && "
            'echo "FUSE and Nexus installed successfully"'
        )
        # Create mount points
        .run_cmd("mkdir -p /home/user/nexus /mnt/nexus")
        .run_cmd("chown -R user:user /home/user/nexus /mnt/nexus")
        # Give user passwordless sudo for FUSE mount operations
        .run_cmd('echo "user ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/nexus')
        .run_cmd("chmod 0440 /etc/sudoers.d/nexus")
        # Enable user_allow_other in fuse.conf for non-root FUSE access
        .run_cmd(
            "sed -i 's/#user_allow_other/user_allow_other/' /etc/fuse.conf 2>/dev/null || "
            'echo "user_allow_other" >> /etc/fuse.conf'
        )
        # Switch back to user
        .set_user("user")
        .set_workdir("/home/user")
    )

    # The start command is inherited from code-interpreter-v1 template
    # which runs: sudo /root/.jupyter/start-up.sh
    # and waits for http://localhost:49999/health

    return template


# Export the template
template = make_template()
