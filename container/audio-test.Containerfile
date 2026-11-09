# Container for tools/audio/test-isolation.sh.
#
# The host has PipeWire but no sox, and (deliberately) no way to run a
# hardware-free second session manager under a sandbox on every machine.  This
# image carries the full toolchain: PipeWire + WirePlumber + pw-* clients,
# python3 with numpy (fast FFT; the stdlib FFT is used when numpy is absent)
# and bubblewrap for the hardware-free session-manager sandbox.
#
# The container has no /dev/snd at all, so the test's WirePlumber can never
# reach the real card even without bubblewrap.
#
#   podman build -t localhost/oxp3-audio-test:f45 -f container/audio-test.Containerfile .
#   tools/audio/test-in-container.sh              # build once, then run the test
#   tools/audio/test-in-container.sh --quick      # shorter sweep
ARG FEDORA=45
FROM registry.fedoraproject.org/fedora:${FEDORA}

RUN dnf -y install --setopt=install_weak_deps=False \
        pipewire \
        pipewire-utils \
        wireplumber \
        python3 \
        python3-numpy \
        bubblewrap \
        procps-ng \
        findutils \
        which \
        coreutils \
    && dnf clean all

# The test creates its own private runtime dir with mktemp; this is only the
# XDG_RUNTIME_DIR the in-container session would otherwise want.
ENV XDG_RUNTIME_DIR=/tmp/audiotest-xdg

WORKDIR /w
