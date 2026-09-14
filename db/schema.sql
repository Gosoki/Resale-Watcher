-- Resale-Watcher 建表脚本。全是 IF NOT EXISTS，可反复执行。
-- 库名要和 .env 的 DB_NAME 一致；改库名时这里和 .env 都要改。
--
-- 【时间列一律由 Python 按 Asia/Tokyo 写入，不用 DEFAULT CURRENT_TIMESTAMP】
-- 理由：DEFAULT CURRENT_TIMESTAMP 取的是 MySQL 服务器的时区，而数据库、开发机、
-- 部署机三边时区不一定一致。各平台给的时间本来就是 JST，
-- 统一在代码里转成 JST 再写，换机器、换服务器时区都不会让历史数据错位。

CREATE DATABASE IF NOT EXISTS resale_watcher
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE resale_watcher;


-- ============================================================
-- 1. 监控规则 —— 这张表是给你填的，其余表都是脚本写的
-- ============================================================
CREATE TABLE IF NOT EXISTS watch_rule (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  name          VARCHAR(64)   NOT NULL                COMMENT '规则名，只给人看，例如「RTX 5090 单卡」',
  enabled       TINYINT(1)    NOT NULL DEFAULT 1      COMMENT '1=启用 0=停用（停用的规则完全不发请求）',

  keyword       VARCHAR(128)  NOT NULL                COMMENT '搜索词，发给所有启用的数据源。宁可宽一点（如「RTX 5090」），精筛交给下面的词表',
  sources       VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '这条规则要搜哪些数据源，逗号分隔（mercari / yahoo_flea / yahoo_auction）。留空=全部源',

  -- 【下面三个词表都是逗号分隔；匹配前会自动归一化：全角→半角、大写→小写、去空格、片假名统一】
  -- 所以填「5090」就能同时命中 RTX5090 / RTX 5090 / ＲＴＸ５０９０ 三种写法，不用自己列变体。
  include_all   VARCHAR(512)  NOT NULL DEFAULT ''     COMMENT '必含词（AND）：全部出现才算匹配。留空=不检查',
  include_any   VARCHAR(512)  NOT NULL DEFAULT ''     COMMENT '任含词（OR）：出现任意一个即可。留空=不检查',
  exclude_any   VARCHAR(1024) NOT NULL DEFAULT ''     COMMENT '排除词：出现任意一个就判不合适。例：ジャンク,部品取り,箱のみ,ノート,ゲーミングPC',

  exclude_sellers VARCHAR(1024) NOT NULL DEFAULT ''   COMMENT '卖家黑名单，逗号分隔的卖家ID。命中就判不合适（reject_reason=seller），但仍入库——取消拉黑后会被重判回来。【每条规则各自一份】和其它词表一致；同一个卖家要在多条规则里分别拉黑。面板命中页每行有「拉黑」按钮，不用手抄ID。【卖家ID未知的商品一律不判】ヤフオク 部分商品不给卖家ID，メルカリShops 的卖家不是用户——不知道≠命中',

  price_min     INT           NOT NULL DEFAULT 0      COMMENT '价格下限（日元，含）。0=不限。低于它多半是配件/废品',
  price_max     INT           NOT NULL DEFAULT 0      COMMENT '价格上限（日元，含）。0=不限。这是「合适」的硬门槛',

  condition_ids VARCHAR(32)   NOT NULL DEFAULT ''     COMMENT '品相白名单，逗号分隔。1=新品未使用 2=未使用に近い 3=目立った傷なし 4=やや傷あり 5=傷や汚れあり 6=全体的に状態が悪い。留空=不限',
  allow_shops   TINYINT(1)    NOT NULL DEFAULT 0      COMMENT '1=也收商家出品（メルカリShops / ヤフオク 的ストア出品，一般是溢价新品）0=只要个人出品。注意 Yahoo!フリマ 的接口不给商家标记，这一项对它不生效',

  check_desc    TINYINT(1)    NOT NULL DEFAULT 1      COMMENT '1=初筛通过后再拉一次商品详情读描述，用下面的 warn_desc 查一遍并打警示标签。关掉就完全不拉详情',
  warn_desc     VARCHAR(1024) NOT NULL DEFAULT ''     COMMENT '描述警示词，逗号分隔，只在描述里查。【命中只打标签，不会把商品毙掉】商品照样进命中列表，面板上带个黄标写明命中了哪个词，你点开自己判断。实测依据：「マイニング」在描述里出现 2 次、2 次都是卖家在否认（「マイニング使用しておらず」），真挖过矿的不会自招——描述词做否决的误杀风险远大于拦截价值',

  deal_price    INT           NOT NULL DEFAULT 0      COMMENT '手动捡漏价（日元）：低于它就算捡漏。【填了就完全盖过 deal_ratio】0=不用手动价，按下面的百分比算。手动价的意义在于它【不依赖成交样本】——刚建规则、或者某个型号成交太少算不出中位数时，百分比那套整个不工作，而你自己心里是有价的',

  deal_ratio    INT           NOT NULL DEFAULT 85     COMMENT '捡漏线（%）：价格低于「近30天成交中位数 × 此值%」时额外标 is_deal。0=不算捡漏，只按价格区间判。deal_price 填了的话这一项不生效',

  quick_min     INT           NOT NULL DEFAULT 7      COMMENT '扫描间隔（分钟）：多久把该关键词的在售商品全扫一遍。实际会在此基础上随机抖动 ±20%',

  note          VARCHAR(255)  NOT NULL DEFAULT ''     COMMENT '备注，随便写',
  created_at    DATETIME      NOT NULL,
  updated_at    DATETIME      NOT NULL,

  KEY idx_enabled (enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='监控规则（人工维护）';


-- ============================================================
-- 2. 规则运行状态 + 市价参考值缓存
-- ============================================================
CREATE TABLE IF NOT EXISTS rule_state (
  rule_id       INT           NOT NULL PRIMARY KEY,
  median_price  INT           NULL                    COMMENT '近30天成交价中位数（日元），【跨源合并】算。样本不足时为 NULL，此时不做捡漏判定',
  sample_count  INT           NOT NULL DEFAULT 0      COMMENT '中位数用到的成交样本数。少于 5 件就不出数，免得被一两件废品带偏',
  updated_at    DATETIME      NOT NULL,
  CONSTRAINT fk_state_rule FOREIGN KEY (rule_id) REFERENCES watch_rule(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每条规则的市价参考值（跨源）';


-- 每条规则在每个数据源上的轮询进度。
-- 【和 rule_state 分开】市价中位数是跨源合并算的（一份），而轮询进度天然是每源一份：
-- Mercari 刚扫完不代表 Yahoo 也扫完了，被限流也是各限各的。
CREATE TABLE IF NOT EXISTS rule_source_state (
  rule_id       INT           NOT NULL,
  source        VARCHAR(16)   NOT NULL,
  last_scan_at  DATETIME      NULL                    COMMENT '上次在售扫描时间（翻页扫全部在售商品：新上架、降价、售出一次全抓）',
  last_sold_at  DATETIME      NULL                    COMMENT '上次成交轮（抓成交价）时间',
  last_total    INT           NOT NULL DEFAULT 0      COMMENT '上次扫描时该关键词在这个源上的在售总数',
  truncated     TINYINT(1)    NOT NULL DEFAULT 0      COMMENT '1=上次扫描被 max_pages 截断没扫全。此时无法判断商品是「卖掉了」还是「只是没翻到」，售出对账会自动跳过',
  last_error    VARCHAR(255)  NOT NULL DEFAULT ''     COMMENT '这个源最近一次出错信息，正常时为空',
  updated_at    DATETIME      NOT NULL,
  PRIMARY KEY (rule_id, source),
  CONSTRAINT fk_srcstate_rule FOREIGN KEY (rule_id) REFERENCES watch_rule(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每条规则在每个数据源上的轮询进度';


-- ============================================================
-- 3. 抓到的商品
-- ============================================================
-- 主键是 (source, item_id, rule_id)：同一件商品若被两条规则同时抓到，两行各自独立判定。
-- 「这个商品在这条规则下合不合适」本来就是两条规则各有答案，合成一行反而要额外的关联表。
-- source 放在最前：绝大多数查询都带 rule_id + source，而不同源的 ID 空间可能撞车。
CREATE TABLE IF NOT EXISTS item (
  source        VARCHAR(16)   NOT NULL                COMMENT '数据源：mercari / yahoo_flea / yahoo_auction',
  item_id       VARCHAR(32)   NOT NULL                COMMENT '该数据源里的商品ID（Mercari 形如 m + 11 位数字，Yahoo 形如 f/z/u + 10 位数字）',
  rule_id       INT           NOT NULL,

  name          VARCHAR(255)  NOT NULL                COMMENT '商品标题',
  price         INT           NOT NULL                COMMENT '当前价（日元）',
  first_price   INT           NOT NULL                COMMENT '首次发现时的价格，用来看降了多少',
  min_price     INT           NOT NULL                COMMENT '跟踪期间见过的最低价',

  status        VARCHAR(12)   NOT NULL DEFAULT 'on_sale' COMMENT 'on_sale=在售 trading=交易中 sold_out=已售出 gone=搜索结果里消失且查无此商品（下架/删除）',
  condition_id  TINYINT       NULL                    COMMENT '品相 1~6，含义见 watch_rule.condition_ids。ヤフオク 的搜索结果不给品相，会是 NULL —— 此时品相白名单【不生效】（不知道 ≠ 不符合，宁可漏筛不可误杀）',
  item_type     VARCHAR(8)    NOT NULL DEFAULT 'user' COMMENT 'user=个人出品 shop=商家出品（メルカリShops 等）',
  category_id   INT           NULL                    COMMENT '该平台的分类ID（各家编号体系不同，不跨源比较）',
  brand_name    VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '品牌名。只有 Mercari 和 Yahoo!フリマ 给，且经常为空或不准；ヤフオク 恒为空',
  seller_id     VARCHAR(32)   NOT NULL DEFAULT ''     COMMENT '卖家ID。【宽度按最长的源定】实测 ヤフオク 的是 28~29 字符，Mercari 是 1~9 位数字，Yahoo!フリマ 是 p+数字共 5~9；原本 VARCHAR(24) 会把 ヤフオク 的全部截断，于是你从网页上复制完整ID填进卖家黑名单会匹配不上',
  thumb_url     VARCHAR(255)  NOT NULL DEFAULT ''     COMMENT '缩略图，面板里显示用',

  listed_at     DATETIME      NULL                    COMMENT '商品上架时间。ヤフオク 的搜索结果不给这个，会是 NULL',
  end_time      DATETIME      NULL                    COMMENT '结束时间。ヤフオク=拍卖截止（到点就没了，最要紧的一个数）；Yahoo!フリマ=出品期限；Mercari 没有',
  bid_count     INT           NULL                    COMMENT '出价数。只有拍卖有；NULL=不是拍卖。>0 说明正在竞价，当前价还会涨',
  buy_now_price INT           NULL                    COMMENT '一口价／即決価格（日元）。0 或 NULL=没有一口价，只能竞价',
  first_seen_at DATETIME      NOT NULL                COMMENT '本脚本首次抓到它的时间',
  last_seen_at  DATETIME      NOT NULL                COMMENT '最后一次在搜索结果里见到它的时间',
  sold_at       DATETIME      NULL                    COMMENT '确认售出的时间',

  matched       TINYINT(1)    NOT NULL DEFAULT 0      COMMENT '1=合适（通过全部规则）0=不合适',
  reject_reason VARCHAR(64)   NOT NULL DEFAULT ''     COMMENT '不合适的原因，如 price_over/price_under/excluded_title/condition/shop_item。调规则时按这个列分组看，能发现自己有没有误杀好货',
  is_deal       TINYINT(1)    NOT NULL DEFAULT 0      COMMENT '1=低于捡漏线（价格 < 成交中位数 × deal_ratio%）',
  deal_pct      INT           NULL                    COMMENT '当前价是成交中位数的百分之多少。80 就是只要市价的八成',

  tracked_at    DATETIME      NULL                    COMMENT '开始追踪的时间。NULL=没在追踪。【追踪的商品会单独拉详情刷新】不等整轮关键词扫描，所以价格/出价数/是否卖掉更新得快得多——代价是每件每次刷新都是一个真实请求，所以有 track_min 间隔和 track_budget 每轮上限两道闸',

  notified_at   DATETIME      NULL                    COMMENT '推送过这件商品的时间。NULL=还没推过。【失败也会写】推送失败不重试：一条迟到一小时的提醒没有意义，而对着挂掉的地址每轮重试会拖慢抓取',
  final_notified_at DATETIME  NULL                COMMENT '推过「拍卖快结束」提醒的时间。NULL=还没推过。【和 notified_at 分开存】第一条是它刚变成捡漏那一刻发的（可能还剩两天），这一条是到点前的临门一脚，两条互不顶替；共用一列的话第二条永远发不出去',

  ship_from     VARCHAR(16)   NOT NULL DEFAULT ''     COMMENT '发货地都道府县，如「東京都」。【只有拉过详情的商品才有】三个源都只在详情响应里给这个字段，搜索结果里没有；没拉过详情的是空串＝未知，不打标签（不知道≠不是）。メルカリShops 的商品详情接口不支持，永远是空',

  desc_checked  TINYINT(1)    NOT NULL DEFAULT 0      COMMENT '1=已拉过详情并用警示词查过描述',
  desc_warn     VARCHAR(128)  NOT NULL DEFAULT ''     COMMENT '描述里命中的警示词，逗号分隔。空=描述干净。【不影响 matched】只是提示你点开看一眼',
  description   MEDIUMTEXT    NULL                    COMMENT '商品描述原文，只有拉过详情的才有',


  PRIMARY KEY (source, item_id, rule_id),
  KEY idx_rule_matched (rule_id, matched, last_seen_at),
  KEY idx_rule_status  (rule_id, source, status),
  KEY idx_first_seen   (first_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='抓到的商品及其判定结果';


-- ============================================================
-- 4. 价格变动历史
-- ============================================================
-- 只在价格真的变了的时候写一行（每轮都写会让这张表毫无信息量地暴涨）。
-- 不带 rule_id：价格是商品自身的属性，和哪条规则抓到它无关。
CREATE TABLE IF NOT EXISTS price_log (
  id        BIGINT AUTO_INCREMENT PRIMARY KEY,
  source    VARCHAR(16) NOT NULL,
  item_id   VARCHAR(32) NOT NULL,
  price     INT         NOT NULL COMMENT '变动后的价格',
  noted_at  DATETIME    NOT NULL COMMENT '观察到这个价格的时间（不是卖家改价的时间，我们看不到那个）',
  KEY idx_item_time (source, item_id, noted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='商品价格变动历史';


-- ============================================================
-- 5. 成交价样本（算市价中位数用）
-- ============================================================
CREATE TABLE IF NOT EXISTS sold_sample (
  id        BIGINT AUTO_INCREMENT PRIMARY KEY,
  rule_id   INT         NOT NULL,
  source    VARCHAR(16) NOT NULL COMMENT '成交样本来自哪个源。中位数是【跨源合并】算的——都是日本二手市场，合并样本量更大更稳',
  item_id   VARCHAR(32) NOT NULL,
  price     INT         NOT NULL COMMENT '成交价（日元）',
  sold_at   DATETIME    NOT NULL COMMENT '成交时间',
  sample_kind VARCHAR(8) NOT NULL COMMENT '怎么收集到的：scan=成交轮扫出来的（只按标题过滤，没拉详情）tracked=我们一直在跟的商品卖掉了（拉过详情，最准）。【和 source 是两回事】source 说的是哪个网站',
  UNIQUE KEY uk_rule_item (rule_id, source, item_id),
  KEY idx_rule_sold (rule_id, sold_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='成交价样本，用来算中位数参考价';


-- ============================================================
-- 5.5 标记（书签）
-- ============================================================
-- 【为什么不做成 item 表上的一列】delete_rule 会 DELETE FROM item WHERE rule_id=…，
-- 也就是删掉一条规则会连它抓过的所有商品行一起清空。标记要是挂在那上面，
-- 你哪天重建规则，攒了几个月的标记就跟着没了 —— 而"记下来以后好查"恰恰是它
-- 唯一的用途，活不过一次改规则就等于没有。
--
-- 【为什么存快照而不是只存 ID】商品会下架、会被平台删、详情页会 404。
-- 只存 ID 的话，以后打开这一页看到的是一排死链接，标题价格全没了。
-- 这几个字段是"你按下标记那一刻看到的东西"，之后不再变。
--
-- 【主键是 source+item_id，不带 rule_id】同一个商品被两条规则抓到是两行 item，
-- 但对你来说它就是一件东西，标记一次就够。
CREATE TABLE IF NOT EXISTS marked_item (
  source     VARCHAR(16)  NOT NULL             COMMENT '哪个源',
  item_id    VARCHAR(32)  NOT NULL             COMMENT '商品在源站的 ID，拼出链接用',
  name       VARCHAR(255) NOT NULL             COMMENT '标记那一刻的标题（快照，之后不跟着源站变）',
  price      INT          NOT NULL             COMMENT '标记那一刻的价格（快照）。以后回看"我当时看到的是多少钱"',
  thumb_url  VARCHAR(255) NOT NULL DEFAULT ''  COMMENT '标记那一刻的缩略图地址（快照）',
  rule_name  VARCHAR(64)  NOT NULL DEFAULT ''  COMMENT '标记时它归在哪条规则下。【存名字不存 rule_id】规则删了这行还得看得懂，存 id 就只剩一个查不到的数字',
  note       VARCHAR(255) NOT NULL DEFAULT ''  COMMENT '你自己写的一句备注，可留空',
  marked_at  DATETIME     NOT NULL             COMMENT '按下标记的时间。列表按它倒序',
  PRIMARY KEY (source, item_id),
  KEY idx_marked (marked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='手动标记的商品（书签）。独立于 item，删规则、商品下架都不影响';


-- ============================================================
-- 6. 每日请求量（风控保险丝）
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_stat (
  day       DATE        NOT NULL COMMENT 'JST 日期',
  source    VARCHAR(16) NOT NULL COMMENT '数据源。分开计数是为了排查「到底是哪个源在烧请求」',
  requests  INT  NOT NULL DEFAULT 0   COMMENT '当天发出的请求数',
  errors    INT  NOT NULL DEFAULT 0   COMMENT '当天失败的请求数（含被限流）',
  PRIMARY KEY (day, source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日请求计数（按源），总量超过上限当天停抓';


-- ============================================================
-- 7. 全局设置 —— 这张表也是给你填的
-- ============================================================
-- 【为什么不写死在代码里】市价样本窗口、抓取节奏、详情预算、每日请求上限……
-- 这些全都是「规则」的一部分，写进 .py 就等于把规则塞进了暗箱：
-- 想把翻页上限从 5 页调到 20 页，得改代码才能生效。
-- 放这里 → 面板「设置」页直接改，10 秒内生效，不用重启。
-- .env 里只剩真正改了必须重启的东西：数据库连接和 Web 端口。
CREATE TABLE IF NOT EXISTS app_setting (
  k          VARCHAR(48)  NOT NULL PRIMARY KEY COMMENT '设置项名',
  v          TEXT         NOT NULL             COMMENT '值。统一按字符串存，读的时候按 config.SETTINGS_SPEC 声明的类型转',
  note       TEXT         NOT NULL             COMMENT '这项是干什么的（建库时从 config.SETTINGS_SPEC 写入）。【必须是 TEXT 不能是 VARCHAR(500)】seed_settings 在启动路径上（main.py → init_schema → seed_settings），说明文字一超长就是 Data too long、整个服务起不来 —— 也就是"给某个设置项多写两行注释"能把进程搞崩。实际撞过一次：给 notify_url 补 Slack 的申请步骤时写到 693 字。',
  updated_at DATETIME     NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='全局设置（人工维护，改了即时生效）';
