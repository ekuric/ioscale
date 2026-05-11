FROM fedora:latest

RUN dnf install -y \
        python3 \
        python3-pyyaml \
        openssh-clients \
        tar \
    && dnf clean all

# Install oc CLI
RUN curl -sL https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz \
    | tar xzf - -C /usr/local/bin oc kubectl \
    && chmod +x /usr/local/bin/oc /usr/local/bin/kubectl

# Install virtctl
ARG VIRTCTL_VERSION=v1.6.4
RUN curl -sL -o /usr/local/bin/virtctl \
        "https://github.com/kubevirt/kubevirt/releases/download/${VIRTCTL_VERSION}/virtctl-${VIRTCTL_VERSION}-linux-amd64" \
    && chmod +x /usr/local/bin/virtctl

WORKDIR /work

RUN mkdir -p results
