FROM cm2network/steamcmd:root
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates lib32gcc-s1 lib32stdc++6 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /pz-server /zomboid /home/steam/Steam \
    && ln -s /zomboid /home/steam/Zomboid \
    && chown steam:steam /pz-server /zomboid /home/steam/Steam
COPY game.py /usr/local/bin/pz-game
USER steam
WORKDIR /pz-server
ENV HOME=/home/steam PYTHONUNBUFFERED=1
EXPOSE 16261/udp 16262/udp 27015/tcp 9090/tcp
ENTRYPOINT ["python3", "/usr/local/bin/pz-game"]
