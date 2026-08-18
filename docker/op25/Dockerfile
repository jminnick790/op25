# OP25 receiver container.
#
# Builds this repo's local op25 source against GNU Radio 3.10 / gr-osmosdr,
# both available as Ubuntu 22.04 apt packages (matches the only GR version
# ./install.sh supports on this branch). The SDR is a remote rtl_tcp server
# reached over the network via gr-osmosdr's rtl_tcp device arg, so no RTL-SDR
# udev rules, USB passthrough, or librtlsdr host driver are needed here.
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        gnuradio \
        gnuradio-dev \
        gr-osmosdr \
        librtlsdr-dev \
        libuhd-dev \
        libhackrf-dev \
        liborc-dev \
        cmake \
        git \
        build-essential \
        pkg-config \
        doxygen \
        clang-format \
        python3-pybind11 \
        python3-numpy \
        python3-waitress \
        python3-requests \
        libsndfile1-dev \
        libspdlog-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /op25
COPY . /op25

# -j is capped rather than using nproc: op25's LTO-enabled Release build
# runs several cc1plus/lto-wrapper instances per job and OOMs on hosts (or
# Docker Desktop VMs) with lots of cores but modest RAM -- reliability
# matters more than build speed for a Docker host that may be modest
# hardware, not a workstation.
RUN echo /usr/bin/python3 > /op25/op25/gr-op25_repeater/apps/op25_python && \
    rm -rf build && mkdir build && cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release .. && \
    make -j2 && \
    make install && \
    ldconfig

WORKDIR /op25/op25/gr-op25_repeater/apps

COPY docker/op25/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# HTTP status/terminal interface
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
