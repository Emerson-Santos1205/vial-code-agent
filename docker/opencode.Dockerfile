FROM node:22-bookworm-slim
RUN npm install --global opencode-ai@1.18.30
WORKDIR /workspace
ENTRYPOINT ["opencode"]
