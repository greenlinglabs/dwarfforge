FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DF_DIR=/opt/dwarf-fortress
ENV SAVES_DIR=/saves
ENV DISPLAY=:99

# Install system dependencies
RUN apt-get update && apt-get install -y \
    wget \
    curl \
    jq \
    xvfb \
    libsdl1.2debian \
    libsdl-image1.2 \
    libsdl-ttf2.0-0 \
    libgl1-mesa-glx \
    libglu1-mesa \
    libopenal1 \
    libgtk2.0-0 \
    python3 \
    python3-pip \
    tar \
    bzip2 \
    && rm -rf /var/lib/apt/lists/*

# Fetch and install DF + DFHack overlay at build time
COPY scripts/get_dfhack_url.py /tmp/get_dfhack_url.py
RUN set -e; \
    URLS=$(python3 /tmp/get_dfhack_url.py); \
    DF_URL=$(echo "$URLS" | sed -n '1p'); \
    DFHACK_URL=$(echo "$URLS" | sed -n '2p'); \
    mkdir -p /opt/dwarf-fortress; \
    echo "==> Downloading Dwarf Fortress: $DF_URL"; \
    wget -q -O /tmp/df.tar.bz2 "$DF_URL"; \
    tar -xjf /tmp/df.tar.bz2 -C /opt/dwarf-fortress --strip-components=1; \
    rm /tmp/df.tar.bz2; \
    echo "==> Downloading DFHack overlay: $DFHACK_URL"; \
    wget -q -O /tmp/dfhack.tar.bz2 "$DFHACK_URL"; \
    tar -xjf /tmp/dfhack.tar.bz2 -C /opt/dwarf-fortress --strip-components=1; \
    rm /tmp/dfhack.tar.bz2 /tmp/get_dfhack_url.py; \
    echo "==> DF install contents:"; ls /opt/dwarf-fortress/

# Remove bundled libs that conflict with system versions on modern Ubuntu
# (DF's bundled libstdc++ is older than what system libGLU requires)
RUN rm -f /opt/dwarf-fortress/libs/libstdc++.so.6 \
          /opt/dwarf-fortress/libs/libgcc_s.so.1

# Back up stock world_gen.txt so we can use it as a template at runtime
RUN cp /opt/dwarf-fortress/data/init/world_gen.txt \
       /opt/dwarf-fortress/data/init/world_gen_stock.txt

# Patch init.txt for headless operation
RUN sed -i 's/\[PRINT_MODE:[A-Z_]*\]/[PRINT_MODE:TEXT]/' /opt/dwarf-fortress/data/init/init.txt && \
    sed -i 's/\[SOUND:YES\]/[SOUND:NO]/' /opt/dwarf-fortress/data/init/init.txt && \
    chmod +x /opt/dwarf-fortress/df || true

# Set up app
COPY web/ /app/
WORKDIR /app
RUN pip3 install -r requirements.txt

# Set up entrypoint
COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Saves volume
VOLUME ["/saves"]

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
