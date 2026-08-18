# AI 中国象棋对弈

融合**顶级引擎（Pikafish）+ 大语言模型（LLM）**混合决策的中国象棋 AI 对弈程序。

引擎提供战术精度，LLM 提供战略判断——分歧时由 DeepSeek 仲裁。支持人类 vs AI、AI vs AI，红黑双方可独立配置不同模型/模式。

## ✨ 功能特性

- 🐟 **Pikafish 引擎接入**：2026-01-02 + 64MiB 大网络（62185→1024→15→32→1），多线程自适应（封顶 16 核）、512MB 换位表、MultiPV 1 走子、死亡自动重启
- 🤖 **LLM 混合决策**：`evaluate_position` / `search_best_move` 双工具由 Pikafish 提供（大师级评估与深搜），LLM 基于引擎参考做战略判断
- ⚖️ **分歧仲裁**：LLM 与引擎不一致时，DeepSeek 第三方仲裁（对称客观事实包，消除信息不对称）
- 📚 **开局库**：64 条常用开局线，前缀加权随机选择（可在左侧面板按红黑独立开关）
- ♟️ **规则引擎**：完整棋规（走法生成/将军检测/长将判决/重复和棋/自然限着 120 步），经 perft 黄金值与 3020 局面走法对拍验证
- 📊 **评估基准**：`scripts/eval_benchmark.py` 量化自研评估 vs Pikafish 的相关性
- 🌐 **视觉模式**：支持将棋盘截图（JPEG base64）发送给支持视觉的模型（DeepSeek 自动禁用）

## 🚀 快速开始

### 环境要求

- Python 3.10+（开发环境 3.13）
- Windows（Pikafish 二进制为 Windows 版）

### 安装与运行

```bash
pip install -r requirements.txt
python main.py
```

首次启动自动初始化 Pikafish（后台线程，不卡界面）。左侧面板选择红黑模型后点击"开始对弈"。

### 模型配置

编辑 `models.json`（复制自 `models.json.example`）：

```json
{
  "models": [
    {
      "id": "qwen3.5-p1",
      "name": "AI",
      "type": "llama-server",
      "endpoint": "http://localhost:8888/v1/chat/completions",
      "model": "qwen3.5"
    },
    {
      "id": "ds4-pro-p1",
      "name": "DS4 Pro",
      "type": "deepseek",
      "endpoint": "https://api.deepseek.com/v1/chat/completions",
      "model": "deepseek-v4-pro",
      "api_key": "${DEEPSEEK_API_KEY}"
    }
  ]
}
```

- `id` 后缀 `-p1`/`-p2` 决定归属红/黑方下拉框；`arbitration` 为仲裁模型（不进玩家下拉框）
- `api_key` 支持 `${ENV_VAR}` 占位符：创建 `.env` 文件（已被 gitignore）或设置环境变量，如 `DEEPSEEK_API_KEY=sk-xxx`
- `type`: `llama-server`（OpenAI 兼容本地端点）/ `deepseek`（DeepSeek API，不支持视觉）
- 每方至少需要一个模型；`models.json` 不存在时回退并提示

## 🎮 AI 模式（红黑独立配置）

| 模式 | 行为 | 适用 |
|------|------|------|
| `hybrid`（默认） | 引擎先跑（Pikafish→MCTS 兜底），结果注入提示词；LLM 决策，分歧仲裁 | 可解释、人机对弈 |
| `search_only` | Pikafish 结果即最终走法，LLM 不干预 | **纯棋力最大化** |
| `llm_only` | 纯 LLM（无引擎参考，工具仅 move_piece） | 演示/弱引擎场景 |

## 🏗️ 项目架构

依赖方向严格单向：`domain/`（棋规与引擎基座）← `services/`、`ai/` ← `app/`（控制器）← `ui/`、`main.py`

```
├── domain/          # 基座：棋规、MCTS、引擎、提示词
│   ├── game.py      #   棋规核心（走法/将军/判决）+ 增量缓存（Zobrist/PST/NNUE 累加器）
│   ├── mcts.py      #   MCTS 搜索引擎（PUCT + LLM 先验）+ 走子/撤销共享工具
│   ├── pikafish.py  #   Pikafish UCI 封装（异步/评估/多主变/自动重启）
│   ├── openings.py  #   64 条开局线
│   └── prompts.py   #   工具定义与提示词（走子/仲裁）
├── ai/              # LLM 工作器：agentic 工具调用循环、文本兜底解析、请求管理
├── app/             # 控制器：状态机、版本门控、引擎桥接（信号中继）、仲裁编排
├── ui/              # Qt 界面（棋盘/面板/日志）
├── services/        # 模型配置加载、日志
├── scripts/         # 评估基准、自对弈数据生成、训练
└── tests/           # 无 GUI 测试套件
```

### AI 决策流程（hybrid 模式）

```
轮到 AI 走子
  → 开局库命中？→ 直接落子（2000ms 间隔）
  → Pikafish 搜索（depth×3s，MultiPV 1，4 线程，512MB Hash）
      → 引擎死亡 → 自动重启 / 失败 → MCTS 兜底（800 sims）
  → 引擎参考注入 LLM 提示词
  → LLM agentic 循环（≤4 轮工具调用）：
      evaluate_position（Pikafish 评估）→ search_best_move（Pikafish 深搜，缺失时 MCTS 兜底）→ move_piece 提交
  → 与引擎一致 → 落子
  → 分歧 → DeepSeek 仲裁（≤180s）→ 落子
  → LLM 失败 → 引擎走法兜底 → 随机
```

## 🧪 测试

```bash
python tests/smoke_engine.py      # 无 GUI 冒烟：走法生成/MCTS/自然限着/开局库/哈希/自弈
python tests/compare_movegen.py   # 走法生成与基线对拍（3020 局面）
python tests/test_perft.py        # Perft 黄金值（44/1920/79666）
python tests/test_evaluation.py   # 评估正确性/对称性/增量等价
python tests/test_incremental.py  # 增量缓存一致性
python tests/measure_vision_image.py  # 视觉截图尺寸/内容覆盖（offscreen Qt，无 GUI）
```

## 🛠️ 脚本工具

```bash
python scripts/eval_benchmark.py            # 评估基准：自研 vs Pikafish 相关性/MAE/符号一致率
python scripts/gen_selfplay.py --games 200 # Pikafish 自对弈数据生成（eval 软标签 + 镜像增强）
python scripts/train_nnue.py --data data/selfplay_data.npz   # 训练自研评估网络
```

## 🔑 技术亮点

- **版本门控体系**：`game_version` + `cancel_version` 双层门控，陈旧回调不重置 busy，杜绝重复走子
- **增量缓存**：Zobrist 哈希 / PST / 子力计数 / NNUE 累加器全程增量维护，搜索热路径零全盘扫描
- **线程纪律**：Pikafish/MCTS/LLM 全部后台线程 + Qt 信号中继回主线程；LLM 工具持锁原子搜索，引擎忙时超时放弃不阻塞
- **快照隔离**：搜索与工具校验全部在局面快照上进行，不触碰 live 棋盘

## 📜 致谢

- [Pikafish](https://github.com/official-pikafish/Pikafish)：开源中国象棋引擎（GPL-3.0），提供大师级棋力与评估
- DeepSeek / LM Studio：LLM 决策与仲裁支持
