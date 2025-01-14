# 使用官方Node.js镜像
FROM node:16-alpine

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY ../frontend/package.json .

# 安装依赖
RUN npm install

# 复制应用代码
COPY ../frontend/index.html .

# 暴露端口
EXPOSE 3000

# 启动命令
CMD ["npm", "start"]