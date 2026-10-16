# ESCI v2 十场景：离线输入绑定

本目录新增四条独立 train 中文查询译文，供既有 `esci-dev-v1` 合同格式绑定 v2 数据。合同格式版本与数据版本不同；生成套件的 `dataset_version` 为 `esci-v2-dev`。

- 作者：OpenAI assistant；在本次助手任务内撰写，没有另发翻译模型 API 请求。运行时未暴露具体模型版本，不伪造型号。
- 四条原文来自 `data/esci/selection-v2.json` 的 train 开发集：us/1567、es/2580、jp/3136、us/2775。
- 没有复制既有 test 中文留出派生缓存。`status=ok` 仅表示译文已经写出；`human_review_status=pending`，不是人工校验结果。
- 保留原查询中的品牌、尺寸、数量和否定。原文“无损”“不伤发”属于查询意图，不能作为商品真实性能承诺；西语 jack 查询仍需真实人工复核歧义。

## 仅离线绑定

```sh
.venv/bin/python eval/harness/esci-v1/build_suite.py \
  --catalog data/catalog-esci-v2.jsonl.gz \
  --selection data/esci/selection-v2.json \
  --ranking eval/esci-v2/ranking-dev.jsonl \
  --pairing-manifest eval/esci-v2/input-pairing.json \
  --translations eval/harness/esci-v2/translations-train-zh-v1.jsonl \
  --index-dir eval/index/esci-harness-v2 \
  --local-model-cache eval/models/multilingual \
  --output eval/verification/<新的套件目录>
```

生成 `suite.json`、`cases.json` 和 `preflight.json`；不执行真实十场景、不创建索引、不调用模型。静态预检不读取凭据或启动模型，因此 `runnable=false`、远端配置 `not_checked`、本地模型 `not_attempted`。即使发现模型文件，也只记录文件指纹，不能宣称已验证可加载。历史 `esci-v1/preparation.json` 保留不改。

## 配对与回滚

正式伴随清单：`eval/esci/input-pairing.json`（v1）与 `eval/esci-v2/input-pairing.json`（v2）。清单绑定 catalog、selection、dev/holdout 三组与中文派生文件的字节数/BLAKE2b、来源提交及内部编号→站点/外部商品身份/SKU 映射。正式清单本身也有固定指纹，不允许临时重算后冒充正式数据。

`run_esci` 默认三件套是 v2；历史 v1 必须显式同时使用 `--catalog data/catalog-esci-v1.jsonl.gz --dataset-dir eval/esci --baseline-version v1`。两者都不改变应用默认 demo 5,700 目录。开发档只核验开发字节；虽然清单列出留出指纹，开发运行不打开留出文件。留出输入被篡改会在 output、配置客户端、索引和 attempt 创建前被拒绝；已进入合法留出运行后发生的失败仍消耗一次尝试。

构造夹具必须使用独立 `constructed_fixture` 清单，并显式传 `--fixture-manifest`；Harness Python 测试入口须显式 `allow_fixture=True`。夹具不能直接引用正式冻结输入路径。正式文件缺少清单时不会自动补签或降级跳过。

若需暂停新实现，停止新的评测运行并保留报告；不要删 attempt、批准基线或改写既有冻结文件来“回滚”。恢复旧 runner 会恢复已知的错配风险，不作为安全替代。后续真实运行仍需独立授权的模型配置、本地 fastembed/模型缓存、隔离索引与最终源码验收；人工 200/50 审核及真实模型 smoke/release 未由本工作完成。
