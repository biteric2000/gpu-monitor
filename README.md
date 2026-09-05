# gpu-monitor
局域网 GPU 节点监控与预约看板。多台装有 NVIDIA GPU（4090/5090 等）的 Windows 机器跑本地 LLM（llama.cpp / vLLM / Ollama 等）时，让团队成员：  1. 在浏览器里看到各机器的 GPU 利用率、显存、温度、功耗、占卡进程； 2. 以「声明」方式独占预约某台机器一段时间，减少撞车。
