"""NiceGUI 面板：改规则、看命中、调排除词。

外观复刻 R-18MediaLibrary 的网页端：左侧导航栏放七个页，顶栏放页名和状态。
七页各对应一件事：
  命中  —— 平时只看这一页：通过全部规则的在售商品，按规则折叠，
            组内按「市价百分比」升序（最划算的排最前）
  追踪  —— 盯住的几件，单独拉详情刷新，比整轮扫描快
  标记  —— 自己标下来的东西，纯记录，商品下架了也还在
  成交  —— 市场实际用什么价清掉了什么货，定价前看分布
  全部  —— 调规则时看这一页：一张平铺表，可按规则/判定原因筛选，
            「具体原因」列会告诉你每一件是被哪个词判掉的
  规则  —— 你自己填的那张表，以及每条规则在各个源上的轮询状态
  设置  —— 全局项（抓取节奏、市价样本窗口、每日上限），改完 10 秒生效
"""
from contextlib import contextmanager
import logging
from datetime import timedelta

from nicegui import run, ui

import config
import sources
from core import notify as push          # 【必须改名】本文件里的 notify() 是弹提示的
from core import poller
from core.matcher import explain
from core.normalize import id_tokens
from db import store

log = logging.getLogger(__name__)

# 暗色样式：ui.dark_mode(True) + ui.colors 定主色 + 这张表修 Quasar 在暗底上的两处短板。
DARK_CSS = (
    "<style>"
    # 【必须显式指定字体族】不指定的话，日文用什么字完全看浏览器挑：
    # 同一页里商品标题（日文）和界面文案（中文）会落到两套不同的字形上，
    # 笔画粗细和字面大小对不齐，看着就是"没做过设计"。
    # 按操作系统各自的系统字排：macOS 走 SF Pro + ヒラギノ，Windows 走 Segoe + 游ゴシック。
    'body,.q-field,.q-btn,.q-table,.q-badge,.q-tab{font-family:'
    'system-ui,-apple-system,"Segoe UI",'
    '"Hiragino Sans","Hiragino Kaku Gothic ProN","Noto Sans JP","Yu Gothic UI","Meiryo",'
    '"PingFang SC","Microsoft YaHei",sans-serif}'
    # 【等宽数字】这是个盯价格的工具，一整列 ¥ 数字不等宽的话，
    # 位数不同的价格左右横跳，扫一眼比大小要重新对焦。
    # 对汉字假名没有任何影响，只管阿拉伯数字。
    # 【15px 是参考项目的基准字号】app.css L10 写死 font-size:15px/line-height:1.5。
    # 行内那些 text-xs / text-sm 都是显式指定的，不受这里影响；变的只是
    # 没写字号的标签 —— 也就是页面上绝大多数说明文字。
    "body{font-size:15px;line-height:1.5;font-variant-numeric:tabular-nums}"
    # Quasar 的卡片/表格默认带投影，那是给亮底设计的；暗底上投影看不见，只会糊成一团。
    # 换成极淡的白色描边来分层。
    # 【底色和圆角必须一起给】只换描边的话，卡片底仍是 Quasar 暗色的 --q-dark
    # #1d1d1d、圆角仍是它默认的 4px —— 而紧挨着的折叠块已经是 #26262b + 10px，
    # 同一页上两种灰、两种圆角。参考端 .card(app.css L96) 是一套：surface 底、
    # 1px line 描边、10px 圆角、零投影。
    ".q-card{box-shadow:none!important;background:var(--rw-surface);"
    "border:1px solid var(--rw-line);border-radius:var(--rw-radius)}"
    ".q-table__container,.q-table__card,.q-table{box-shadow:none!important}"
    ".q-table tbody td,.q-table thead th{font-size:14px}"
    # 命中列表每行之间的分隔线：Tailwind 的 border-t 默认取亮底灰，暗底上几乎是白的
    ".border-t{border-top:1px solid var(--rw-line)!important}"
    "@layer overrides{"
    # 徽标背景统一到 Tailwind -400 档（和文字色同一套），免得 Quasar 原色在暗底上过饱和
    # 【这 6 枚直接取自参考项目】src/Themes/Dark.xaml L28-33 的状态色刷。
    # 它那套 Web CSS 只定义了危险红，剩下五枚在 WPF 主题里 —— 两边的红
    # 本来就是同一个 #f87171，说明这 6 枚和 Web 令牌是配套调出来的。
    # 【语义以调用点为准，别照着色名猜】下面每条后面写的是它【实际】挂在哪些徽标上，
    # 是 grep 过 ui.badge() 的调用点核出来的 —— 之前这里按色名想当然写过一轮，
    # 错了三条（把 orange 写成"已降价"、amber 写成"新上架"、red 写成"警示"）。
    ".q-badge.bg-green{background:#4ade80!important}"                        # 捡漏、曾是捡漏
    ".q-badge.bg-orange{background:#fb923c!important}"                       # 新上架
    ".q-badge.bg-amber{background:#facc15!important}"                        # 描述警示、交易中
    ".q-badge.bg-grey{background:var(--rw-muted)!important}"                 # 中性：失联/已卖掉/已下架/停用
    # 【是 blue-grey 不是 teal】「新发现」那枚写的是 color="blue-grey"，
    # 全项目没有一处 color="teal" —— 写成 .bg-teal 的话这条选不中任何元素，
    # 那枚徽标会独自留在 Quasar 原色 #607d8b 的白字上，还漏掉下面那条深字规则。
    ".q-badge.bg-blue-grey{background:#60a5fa!important}"                    # 新发现
    ".q-badge.bg-red{background:#f87171!important}"                          # 已降 ¥xxx
    # 【这半条和上面是同一个决定】Quasar 的 .q-badge{color:#fff} 是写死的白字，
    # 而上面这批背景亮度在 70%~83% —— amber 上白字只有 1.7:1、green 2.2:1，全线看不清。
    # 亮底一律改配深色前景。
    ".q-badge.bg-green,.q-badge.bg-orange,.q-badge.bg-amber,.q-badge.bg-grey,"
    ".q-badge.bg-blue-grey,.q-badge.bg-red,.q-badge.bg-purple{color:#18181b!important}"
    # 【来源是标签，不是信号】メルカリ/ヤフオク 这类徽标回答的是"它在哪"，
    # 而捡漏/已降/新上架回答的是"要不要点进去"。两者同样是实心色块的话，
    # 一行里五六个色块抢注意力，真正的信号反而沉下去了。
    # 改成描边：同样占位、同样可读，但视觉重量降一档。
    ".q-badge.badge-label{background:transparent!important;color:var(--rw-muted)!important;"
    "border:1px solid var(--rw-line);font-weight:400}"
    # Quasar 的按钮为大写英文留了字距，中日文标签上只会显得松散
    ".q-btn{letter-spacing:0}"
    # 【淡化的危险按钮】拉黑这类操作每行都有一个，全红会把整页压成一片红。
    # 平时压到 40% 不透明度，鼠标移上去才满 —— 要用时找得到，不用时不碍眼。
    # 【原先注释写 40% 而代码是 .55，两边一直对不上】按钮统一尺寸之后
    # 「拉黑卖家」从 size=sm 长回默认大小，右边整整一列红字明显抢眼了，
    # 正好把它调回注释里本来就说好的 40%。层级靠颜色和透明度，不靠大小。
    ".btn-muted{opacity:.4;transition:opacity .15s}"
    # 【行内操作按钮跟左边那行小字同号】「剔除」「拉黑卖家」和左下角的
    # 「来源 · 品相 · 发货 · 上架」是同一条基线上的两头，字号不一样的话
    # 两边一眼看过去不像一层，右边那两个显得比实际重要。
    # 【必须连 .q-btn__content 一起写】Quasar 的字号定在 .q-btn 上，
    # 而按钮文字实际在内层的 .q-btn__content 里 —— 只写外层，内层会
    # 从 Quasar 那边继承回 14px，等于没改。min-height 同理：不压下来的话
    # 字变小了按钮框还是原来那么高，两边基线照样对不齐。
    ".row-act,.row-act .q-btn__content{font-size:.75rem;line-height:1rem}"
    ".row-act{min-height:20px;padding:0 6px}"
    # 【图片角上的星】收藏手势大家都认得，比底部一个文字按钮自然得多。
    # 商品图什么底色都有（白盒、亮桌面、深机箱），所以星必须自带描边阴影，
    # 否则压在浅色图上直接消失。
    # 【颜色必须连 .q-btn__content 一起写、还要 !important】Quasar 的按钮没指定
    # color 时默认用主色，只给外层 .star-btn 上色会被它盖掉 —— 星会渲染成蓝的。
    # 和 BTN_QUIET 当初那个坑同源：flat 按钮的颜色默认不是继承来的。
    # 【必须是正方形，否则圆角画出来是椭圆】Quasar 的 .q-btn--round 是这么写的：
    #   border-radius:50%; padding:0; min-width:2.4em; min-height:2.4em
    # 那三条一起把盒子锁成正方形，50% 圆角才是正圆。我们原先把三条全用
    # !important 覆盖掉了（min-* 归零 + padding 给成 1px 4px 的不对称值），
    # 盒子变成长方形 —— 而且两个角标椭的方向还相反：★ 字面宽，被压成横椭圆；
    # ⚑ 字面窄，被拉成竖椭圆。平时 flat 透明看不出来，鼠标一上去 hover 底色
    # 填满整个盒子就露馅了。所以尺寸要给死 width=height，padding 归零。
    ".star-btn{min-width:0!important;min-height:0!important;padding:0!important;"
    "width:26px;height:26px;font-size:18px;line-height:1;opacity:.8;"
    "text-shadow:0 0 3px rgba(0,0,0,.95),0 1px 4px rgba(0,0,0,.9);transition:all .15s}"
    ".star-btn,.star-btn .q-btn__content{color:#fff!important}"
    ".star-btn:hover{opacity:1;transform:scale(1.2)}"
    # 已追踪用琥珀实心星：和「捡漏」的绿、「已降」的红都不撞，而且实心/空心
    # 本身就能一眼看出状态，不必靠颜色分辨。
    ".star-on{opacity:1}"
    ".star-on,.star-on .q-btn__content{color:oklch(82.8% 0.189 84.429)!important}"
    # 标记旗用青色：和追踪的琥珀、捡漏的绿、已降的红都拉得开。
    # 【必须写在 .star-btn 之后】那条规则也带 !important 且特异性相同，
    # 同分时靠出现顺序决胜 —— 写在前面的话旗子会被刷成白的。
    # 【旗要单独调两次：先补大小，再补笔画】★ 和 ⚑ 是两个不相干的字符，
    # 同一字号下旗的字面小一圈、笔画还更细 —— 大小和粗细是两回事，
    # 只放大字号会得到一个"又大又细"的旗，摆在星旁边照样不像一套。
    #   font-size:22px        补字面大小（18px 的星 ≈ 22px 的旗）
    #   -webkit-text-stroke   补笔画粗细，.5px 时和 ☆ 的描边几乎等重
    # 两个数都是把星和旗挨着放、在 5 倍图上逐档比出来的，不是估的。
    # 【text-stroke 挂了也不会坏】不支持的浏览器只是旗细一点，不影响功能。
    ".mark-btn{font-size:22px;-webkit-text-stroke:.5px currentColor}"
    ".mark-on{opacity:1}"
    ".mark-on,.mark-on .q-btn__content{color:oklch(78.9% 0.154 211.53)!important}"
    ".btn-muted:hover{opacity:1}"
    # 東京都 原先用 teal，和「捡漏」的绿是相邻色相，扫一眼分不开，而这俩语义
    # 完全不同（一个说地点、一个说机会）。换到紫档，和绿/橙/红/琥珀/灰都拉开。
    ".q-badge.bg-purple{background:#a78bfa!important}"                       # 東京都
    # 【没有这一条，设置页那根 sticky 保存条是死代码】Quasar 的标签面板套了两层
    # 滚动容器：.q-tab-panels.q-panel-parent 是 overflow:hidden，里面每个
    # .q-panel 还带 class="scroll"（overflow:auto）。position:sticky 的偏移参照
    # 「最近的滚动祖先」而不是视口，于是那根条子参照的是一个【永远不滚】的盒子
    #（面板没设高度、高度跟内容一起长，scrollHeight == clientHeight），
    # 偏移量恒为 0，sticky 退化成 relative。在真实页面上实测过：滚 600px 后
    # 条子 top=-414，也就是跟着内容一起滚走，和没写 sticky 完全一样；
    # 放开这两层之后同样的实测是 top=45、挡路祖先=[]。
    # 【必须配合 animated=False】切标签页时 Quasar 会把两个面板绝对定位叠在一起，
    # overflow:hidden 正是用来裁掉它的；不关动画就会看到两页互相穿帮。
    ".q-tab-panels.q-panel-parent,.q-tab-panels .q-panel.scroll{overflow:visible}"
    # 【次操作按钮的字要用亮档，不能用主色】BTN_GHOST 是 flat+color=primary，
    # 而主色现在是参考端的【填充档】#4a78c0 —— 那个色是拿来铺底的，当文字压在
    # #1d1d1d/#26262b/#2f2f36 上只有 3.0〜3.8:1，全线不过 AA（换主色之前是 6.6〜7.4）。
    # 参考端自己就把强调色分成两档：填充用 --accent-dim、文字和链接用 --accent，
    # 这里正是那条规矩落地的地方。
    # 【必须写在 @layer overrides 里、且带 !important】Quasar 的
    # .text-primary{color:var(--q-primary)!important} 在 quasar_importants 层。
    # !important 的层序是【反】的：先声明的层赢 —— overrides 排在 quasar_importants
    # 前面，所以这里压得住；写在本文件那段无层 CSS 里则永远压不住。
    # 【必须排除 round】缩略图角上的 ★ ⚑ 和顶栏 ☰ 都是 BTN_CORNER（flat round、
    # 不指定 color），Quasar 照样给它们挂 text-primary。不排除的话这条（0,3,0）
    # 压过 .star-btn/.star-on/.mark-on（0,2,0）—— 同在这一层、同带 !important，
    # 比的就是特异性：白星白旗全变蓝，【已追踪的琥珀星、已标记的青旗也变蓝】，
    # 缩略图上再也分不出追没追、标没标。上一轮就栽在这儿，测试锁着。
    ".q-btn--flat.text-primary:not(.q-btn--round),"
    ".q-btn--flat.text-primary:not(.q-btn--round) .q-btn__content"
    "{color:var(--rw-accent)!important}"
    # 【工具条上的按钮字一律用正文色】参考端 .icon-btn 是 color:var(--text)：
    # 有了底色和描边，按钮已经一眼认得出，不需要再靠蓝字区分；「主操作」那一档
    # 靠的是实心蓝底（BTN_PRIMARY），不是字色。原先「刷新」是 grey-5 的 #bdbdbd，
    # 「测试推送」是蓝字 —— 同一排两个扁按钮两种字色。
    # 【必须写在上一条之后】两条特异性相同，同分靠顺序。
    ".rw-toolbar .q-btn--flat,.rw-toolbar .q-btn--flat .q-btn__content"
    "{color:var(--rw-text)!important}"
    # 顶栏 ☰ 用正文色。同样得写在这一层：它也挂着 text-primary 的 !important。
    ".rw-header .q-btn,.rw-header .q-btn .q-btn__content{color:var(--rw-text)!important}"
    "}"
    # ══════════════════════════════════════════════════════════════════════
    # 【以下整块复刻自 R-18MediaLibrary 的 src/Web/app.css】那是它自带的局域网
    # 网页端，和它的 WPF 桌面端共用一套设计语言。我们同样是网页，所以抄 Web 那份
    # 而不是 XAML —— 它已经把 backdrop-filter、rgba 遮罩这些只有浏览器才有的
    # 东西调好了；XAML 那套色偏冷蓝（#15171c/#232834），而截图上的实际观感是
    # 中性灰（取过像素核对：#1b1b1f/#26262b/#4a78c0 三个点分毫不差）。
    #
    # 【为什么这一整块不写 !important】NiceGUI 在 index.html 里声明了层序：
    #   @layer theme, base, quasar, nicegui, components, utilities, overrides, quasar_importants;
    # 而 add_head_html 注进来的这段【不属于任何层】。CSS 的规矩是两套相反的：
    #   普通声明 —— 无层胜过所有层，所以这里不加 !important 就已经压得住 Quasar；
    #   !important —— 层序反转，无层反而【输给】任何层里的 important。
    # 所以硬碰 Quasar 的 important（比如主色按钮底色）在这儿是写不赢的，
    # 那一处改走 ui.colors() 直接换 --q-primary。
    # ══════════════════════════════════════════════════════════════════════
    # 设计令牌：app.css L2-4 原样搬来，加 rw- 前缀避开 Quasar 自己的 --q-*。
    # 【强调色有亮暗两档，分工不能混】--rw-accent 亮档只用于 focus 描边和链接，
    # --rw-accent-dim 暗档用于大面积填充（激活的导航项、主按钮）。
    # 暗底上用亮档铺满一整条导航项会刺眼，这是那份 CSS 分两档的全部理由。
    ":root{--rw-bg:#1b1b1f;--rw-surface:#26262b;--rw-surface2:#2f2f36;--rw-line:#3a3a42;"
    "--rw-text:#e8e8ea;--rw-muted:#9a9aa3;--rw-accent:#6aa3ff;--rw-accent-dim:#4a78c0;"
    "--rw-radius:10px}"
    # 页面底 / 主区底。Quasar 暗色给的是 #121212 和 #1d1d1d，比参考端各深/亮一档
    "body,.q-page-container{background:var(--rw-bg);color:var(--rw-text)}"
    ".q-tab-panels,.q-tab-panel{background:transparent}"
    # 【主区内距只能有这一层】NiceGUI 的页面根 .nicegui-content 自带 16px 内距和
    # 16px 子元素间距；而它的第一个孩子是那个高度为 0 的 dialog_host（对话框的家），
    # 间距照样算 —— 于是顶栏和工具条之间凭空多出 16+16=32px 的空带，左右也各多 16px。
    # 实测工具条顶在 y=89，参考端的内容顶在 45+12=57。两层都归零，只留下面这一层
    # 12px，才是参考端 main{padding:12px} 的样子。
    ".nicegui-content{padding:0;gap:0}"
    ".q-tab-panel{padding:12px 12px 40px}"            # app.css L74 main{padding:12px … 40px}
    # ---------- 左侧导航栏（app.css L39-49 .nav / .navitem）----------
    # 【QDrawer 的 class 落在内容层不是壳层】它是 inheritAttrs:false，把 attrs
    # 展开到内层的 .q-drawer__content 上 —— 所以 .nav-drawer 选中的是内容层，
    # 壳层要另外用 aside.q-drawer 兜一句，否则 .q-dark 的 #1d1d1d 会从边上透出来。
    "aside.q-drawer{background:var(--rw-surface)}"
    ".q-drawer__content.nav-drawer{background:var(--rw-surface);"
    "border-right:1px solid var(--rw-line);padding:12px 10px;gap:4px;align-items:stretch}"
    ".nav-brand{font-weight:700;font-size:16px;line-height:1.2;padding:6px 10px 14px;"
    "color:var(--rw-text)}"
    # 【导航项 = 被改造过的 QTab】indicator 是 QTab 永远渲染的一个 div，
    # indicator-color 只改颜色不改存在，所以直接 display:none 换成整块底色。
    # 【height:auto 不是可选项】q-tabs--vertical 默认 height:100%，七项一旦装不下
    # 就会冒出上下两个滚动箭头（暗底上是两团白光斑）。让它按内容长，滚动交给抽屉本身。
    ".nav-tabs.q-tabs--vertical{height:auto}"
    ".nav-tabs .q-tabs__content{height:auto;overflow:visible;gap:4px}"
    ".nav-tabs .q-tab__indicator{display:none}"
    ".nav-tabs .q-tab{min-height:0;padding:10px 12px;border-radius:8px;"
    "justify-content:flex-start;text-align:left;color:var(--rw-muted);"
    "transition:background-color .12s,color .12s}"
    # 【左对齐要靠 align-items 而不是 justify-content】.q-tab__content 是
    # flex-direction:column（竖着放图标和文字），主轴是纵向 —— justify-content
    # 管的是上下，横向得用 align-items。之前两条都写在 justify-content 上，
    # 于是文字一直是居中的，和参考端 .navitem 的左对齐对不上。
    ".nav-tabs .q-tab__content{padding:0;min-width:0;width:100%;"
    "justify-content:center;align-items:flex-start}"
    # 字号定在 __label 上，改外层 .q-tab 无效 —— 和下面 .row-act 那个坑同源
    ".nav-tabs .q-tab__label{font-size:14.5px;font-weight:500;line-height:1.5}"
    ".nav-tabs .q-tab--inactive{opacity:1}"          # 默认 .85，改用颜色区分深浅
    ".nav-tabs .q-tab:hover{background:var(--rw-surface2);color:var(--rw-text)}"
    ".nav-tabs .q-tab--active{background:var(--rw-accent-dim);color:#fff}"
    # 激活项已经是蓝底了，Quasar 的 hover 白膜(.q-focus-helper 15%)只会把它冲淡
    ".nav-tabs .q-tab--active .q-focus-helper{display:none}"
    # 【焦点指示要单独补回来】上一条为了不让白膜冲淡蓝底，把激活项的
    # .q-focus-helper 关掉了；而 Quasar 的 .q-focusable{outline:0} 把原生轮廓也去了，
    # 两下一叠，键盘 Tab 进侧栏正好落在【激活项】上却全程没有任何提示。
    # :focus-visible 只在键盘操作时亮，鼠标点击不会触发，不影响观感。
    ".nav-tabs .q-tab:focus-visible{outline:2px solid var(--rw-accent);"
    "outline-offset:-2px}"
    # ---------- 顶栏（app.css L15-24 header/.topbar/.title）----------
    # 【高度必须钉死 45px】设置页那根 sticky 保存条写的是 top-[45px]，
    # 那个数就是实测出来的顶栏高。这里一松，那根条子立刻钻到顶栏底下。
    ".q-header.rw-header{background:rgba(27,27,31,.96);backdrop-filter:blur(8px);"
    "-webkit-backdrop-filter:blur(8px);border-bottom:1px solid var(--rw-line);"
    "box-shadow:none;color:var(--rw-text);min-height:45px}"
    # 【下面两条才是"45px"真正成立的原因，缺一条都不成立】
    # min-height 只是下限、不封顶，光靠它什么都钉不住：
    #   ① ☰ 是 flat dense round，Quasar 给的 min-height 是 2.4em=33.6px，
    #      加顶栏 py-2 的 16px 加 1px 下边框正好 50.6px —— 只要窗口 <1024px
    #      （☰ 因 lt-md 出现）顶栏就必然超高，和文案长短无关。压成 28px：28+16+1=45。
    #   ② 状态那行字在窄屏会折成两行，一折就是 98px。改成截断不折行。
    # 【为什么非要 45px 不可】设置页那根 sticky 保存条写死 top-[45px]；顶栏一旦更高，
    # 条子就整条缩在顶栏底下 —— 手机上「保存全部」直接按不着（实测点到的是顶栏的状态字）。
    ".rw-header .q-btn{min-height:28px;min-width:28px;width:28px;height:28px;padding:0}"
    ".rw-header .text-sm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"
    "min-width:0}"
    # 【flex-shrink:0】顶栏不折行之后，窄屏上空间不够时 flex 会把所有子元素一起压 ——
    # 页名跟着状态字被压成「命」。该截断的只有状态字（尾巴是时间戳），页名最多两个字。
    ".rw-title{font-weight:600;font-size:16px;overflow:hidden;text-overflow:ellipsis;"
    "white-space:nowrap;flex-shrink:0}"
    # ---------- 每页顶上那一行（app.css L76 .toolbar / L75 .count）----------
    # 参考端的 .icon-btn 是「有底色的扁按钮」：surface2 底 + 1px 描边，
    # hover 只换描边色、底色不动。这个质感只给页首工具条上的按钮 ——
    # 行内那些「剔除 / 拉黑卖家」是刻意做轻的，套上底色会把整页压满按钮框。
    # 字色不写在这里：工具条上的扁按钮全都带 color=（grey-5 / primary），Quasar 会挂
    # .text-* 的 !important，这段无层 CSS 压不住 —— 在这里写 color 是条死声明。
    # 真正生效的那条在上面 @layer overrides 里。
    ".rw-toolbar .q-btn--flat{background:var(--rw-surface2);"
    "border:1px solid var(--rw-line)}"
    ".rw-toolbar .q-btn--flat:hover{border-color:var(--rw-accent-dim)}"
    ".rw-desc{color:var(--rw-muted);font-size:13px}"
    # ---------- 控件（app.css L21-36 .icon-btn / input）----------
    # 【必须排除 round】.q-btn--round 是 border-radius:50%，被这条盖掉的话
    # 缩略图角上的 ★ ⚑ 会从正圆变成圆角方块 —— 那两颗的尺寸注释里已经为
    # 类似的事栽过一次（椭圆那次）。
    ".q-btn:not(.q-btn--round){border-radius:8px}"
    ".q-field--outlined .q-field__control{border-radius:8px;background:var(--rw-surface2)}"
    ".q-field--outlined .q-field__control:before{border:1px solid var(--rw-line)}"
    ".q-field--outlined .q-field__control:hover:before{border-color:var(--rw-accent-dim)}"
    # 【hover 用暗档、focus 用亮档】这是参考端全站唯一区分两档强调色的地方
    ".q-field--outlined.q-field--focused .q-field__control:after{"
    "border:1px solid var(--rw-accent)}"
    ".q-field__native,.q-field__input{color:var(--rw-text)}"
    ".q-field__label,.q-field__prefix,.q-field__suffix{color:var(--rw-muted)}"
    # 徽标取参考端 .pill 的形（L278：全圆角、12px），不是贴图上的 .badge（6px 圆角）——
    # 我们的徽标是跟在文字行里的，不压在缩略图上
    # 【不要在这里定 font-weight】参考端的 .pill(L278) 本来就不定字重；而且这段是
    # 无层的，一旦定了就会反过来压死 @layer overrides 里 .badge-label 的
    # font-weight:400（无层普通声明胜过所有层 —— 这条规矩对自家旧规则同样成立），
    # 「来源是标签不是信号、视觉重量降一档」那个设计就丢了一半。
    ".q-badge{border-radius:999px;padding:2px 8px;font-size:12px;line-height:1.4}"
    # ---------- 容器（app.css L96-98 .card / L285 .sec）----------
    # 【折叠块不加 overflow:hidden】参考端的 .card 是带的，但 overflow 一旦不是
    # visible 就会成为 position:sticky 的包含块 —— 设置页那根保存条正是栽在
    # 这类祖先上过一次。圆角那点溢出肉眼看不见，这个风险不值得换。
    ".q-expansion-item{background:var(--rw-surface);border:1px solid var(--rw-line);"
    "border-radius:var(--rw-radius);transition:border-color .12s}"
    ".q-expansion-item .q-item__label--caption{color:var(--rw-muted)}"
    # 【必须掐掉 Quasar 的 hover 白膜】它是 .q-focus-helper：鼠标移到折叠块头上时
    # 铺一层 currentColor、15% 不透明度。问题出在它的 border-radius 是 0，而折叠块是
    # 10px 圆角、上面那条注释又说明了不能给容器加 overflow:hidden —— 于是这层方角的膜
    # 直接糊出圆角外面：实测 hover 时圆角外本该是页底 #1b1b1f，实际被染成 #3b3b3e，
    # 弧内侧更亮到 #55555c，看上去就是角上有一团脏阴影。
    # 【换成参考端的做法】那套 CSS 里根本没有"灰膜"这种东西，可点的表面 hover 一律
    # 只换描边色（app.css L98 .card:hover{border-color:var(--accent-dim)}）——
    # 描边跟着容器自己的圆角走，不存在戳出来的问题。
    ".q-expansion-item .q-item:hover > .q-focus-helper{opacity:0}"
    ".q-expansion-item:hover{border-color:var(--rw-accent-dim)}"
    # ---------- 表格（app.css L272-277 .dt）----------
    ".q-table thead th{color:var(--rw-muted);font-weight:500;background:var(--rw-bg);"
    "border-bottom:1px solid var(--rw-line)}"
    ".q-table tbody td{border-bottom:1px solid var(--rw-line)}"
    ".q-table tbody tr:hover td{background:var(--rw-surface)}"
    # ---------- 浮层 / 链接 ----------
    # 【这条必须进 @layer components】无层的 a{} 会连 <a class="text-blue-400"> 这种
    # 行内指定的 Tailwind 颜色类一起压掉（普通声明先比层再比特异性，无层最大），
    # 表现是"改了没反应"且不报错。放进 components 层就排在 utilities 前面，
    # 既压得住 NiceGUI 的默认链接色，又让行内写的颜色类照常生效。
    "@layer components{a{color:var(--rw-accent)}}"
    ".q-dialog .q-card,.q-menu{background:var(--rw-surface);"
    "border:1px solid var(--rw-line);border-radius:var(--rw-radius)}"
    ".q-menu .q-item:hover{background:var(--rw-surface2)}"
    ".q-dialog__backdrop{background:rgba(0,0,0,.55)}"      # app.css L408 .overlay
    # 【参考端全站只有三处投影，这里是其中两处】它的分层一律靠 1px 描边，
    # 只有"浮在页面之上"的东西才给投影：移动端抽屉和弹出层。照抄它的值。
    ".q-drawer--on-top{box-shadow:2px 0 18px rgba(0,0,0,.55)}"   # app.css L62 移动抽屉
    ".q-menu{box-shadow:0 6px 24px rgba(0,0,0,.4)}"              # app.css L372 .apopup
    # 【抽屉遮罩这一处抄不了，也不该硬抄】参考端 .drawer-backdrop 是 rgba(0,0,0,.5)，
    # 而 Quasar 把遮罩色写成【内联样式】（滑动关闭时要一路渐变它的透明度），
    # CSS 写 .q-drawer__backdrop{background:...} 是条死规则、根本不生效。
    # 加 !important 确实压得住内联，但会把那段拖拽渐变冻死 —— 为 0.1 的透明度差
    # 换掉一个手势动画不划算。留着 Quasar 的 .4，差别肉眼看不出来。
    ".q-tooltip{background:var(--rw-surface2);color:var(--rw-text);"
    "border:1px solid var(--rw-line);font-size:12.5px}"
    # 滚动条：参考端没定义（它跑在 Windows 上，系统滚动条本来就是暗的），
    # 而 macOS/Linux 的浏览器会画一条亮灰，贴在 #1b1b1f 上是一道白杠。
    # 这是复刻之外我们自己补的一处，取的是同一套令牌。
    "::-webkit-scrollbar{width:10px;height:10px}"
    "::-webkit-scrollbar-track{background:var(--rw-bg)}"
    "::-webkit-scrollbar-thumb{background:var(--rw-line);border-radius:6px}"
    "::-webkit-scrollbar-thumb:hover{background:var(--rw-muted)}"
    # 【把 Tailwind 的灰接到参考端的 --muted 上】全站 38 处说明文字写的是
    # text-gray-400 = #9ca3af，而参考端的次要文字是 #9a9aa3 —— 差别只在
    # 那一点蓝味，但正是这点蓝味构成了它那套色和 Tailwind 灰阶的分界
    #（参考端整套是中性灰，WPF 那套才是冷蓝灰）。改一条比改 38 处干净。
    # 【只接 400 这一档】gray-300/500 在我们这儿是刻意的第三档（分区标题、
    # 设置项脚注），参考端信息密度没这么高、只有两档，接过去会把层级压平。
    ".text-gray-400{color:var(--rw-muted)}"
    "</style>"
)

# 发货地标签只标这一个 —— 你要的是「在不在东京都内」，其余都道府县
# 照常显示在下面那行小字里，但不占徽标位。
TOKYO = "東京都"

# 【按钮只有这几种角色，别再发明第四种】改之前这里有 8 种 props 写法，
# 有的带 dense 有的不带（高度差一截）、有的浮起有的扁平，同一行里就能看出参差。
# 统一到角色之后，加新按钮时照抄一个常量即可，不用再逐个拍板。
# 【尺寸只有一个，别再加 size=】层级靠颜色和填充区分，不靠大小。
# 之前「设置规则」「拉黑卖家」带着 size=sm，而紧挨着它们的「保存」是默认尺寸 ——
# 两个按钮并排差一档，一眼就看出来没收拾过。tests/test_ui_consistency.py 拦着这条。
BTN_PRIMARY = "unelevated dense no-caps color=primary"        # 主操作：一键抓取、保存
BTN_GHOST = "flat dense no-caps color=primary"                # 次操作：设置、编辑、跑一轮
# 【必须显式给灰色】flat 不指定 color 时 Quasar 默认用主色 —— 那 BTN_QUIET 和
# BTN_GHOST 渲染出来一模一样，三档层级塌成两档，"轻操作"这一档等于没做。
BTN_QUIET = "flat dense no-caps color=grey-5"                 # 轻操作：刷新、取消、设置规则
BTN_DANGER = "flat dense no-caps color=negative"              # 危险：拉黑、删除
BTN_DANGER_SOLID = "unelevated dense no-caps color=negative"  # 危险且要确认：删除对话框
# 纯图标按钮（缩略图角上的追踪星/标记旗，以及窄屏顶栏那颗 ☰）。它不属于上面
# 那几档 —— 没有文字、没有底色、是圆的，所以单列一个常量而不是硬写。
# 【不要为 ☰ 再开一个常量】那会是两个值完全相同的常量，正好违反上面那条
# 「别再发明第四种」—— 角色是同一个：图标、扁平、圆形。
BTN_CORNER = "flat dense round"                               # 纯图标：★ 追踪、⚑ 标记、☰ 导航
INPUT = "dense outlined"                                      # 所有输入框

# 徽标分两类：信号（实心，抢眼）和标签（描边，只说明"它是什么"）
BADGE_LABEL = "badge-label"

# 卡片和折叠块的外边距只有这一个值。原先是 my-1 / my-2 / mb-3 三种混着，
# 纵向节奏在页面之间对不上 —— 从设置页切到规则页能看出行距在跳。
CARD = "w-full my-2"

COND = {1: "新品未使用", 2: "未使用に近い", 3: "傷汚れなし",
        4: "やや傷汚れ", 5: "傷や汚れあり", 6: "状態が悪い"}

REASON_LABEL = {
    "": "合适", "price_over": "超预算", "price_under": "低于下限",
    "excluded_title": "标题排除词",
    # no_keyword 平时不会入库（第一层判定直接丢弃），但 poller.revalidate 会用当前规则
    # 重判【已在库】的商品 —— 你把必含词改严之后，老商品就会被写成这个原因留在库里
    "no_keyword": "关键词不匹配",
    "condition": "品相不符", "shop_item": "Shops商家品",
    "seller": "卖家拉黑",
}

# 编辑对话框里每个字段的提示。写在这里而不是只靠 DDL 注释 ——
# 填表的人在面板上，不会去翻 SHOW FULL COLUMNS。
FIELD_HELP = {
    "name": "规则名，只给人看",
    "keyword": "搜索词，发给所有启用的数据源。宁可宽一点，精筛交给下面的词表",
    "include_all": "必含词（全部要有）。填「5090」就能命中 RTX5090 / RTX 5090 / ＲＴＸ５０９０",
    "include_any": "任含词（有一个就行）。留空=不检查",
    "exclude_any": "标题排除词，命中任一即判不合适。整机靠 CPU 型号（ryzen/ultra9/14900）最好认",
    "warn_desc": "描述警示词，只查描述。【命中只打黄标，不会毙掉商品】所以可以放宽一点填，"
                 "宁可多挂个标签让你看一眼，也别静悄悄漏掉一块好卡",
    "condition_ids": "品相白名单 1新品〜6状态差，逗号分隔。留空=不限",
    "note": "备注",
}


# 顶栏判「轮询已停」的阈值（分钟）。看门狗用的是设置里的 poller_stall_min，
# 顶栏要更灵敏一点：你正看着面板，早 7 分钟知道没有坏处。
TOPBAR_STALL_MIN = 3


def fmt_minutes(mins: int) -> str:
    """分钟数 → 「N 分钟」「N 小时 M 分」「N 天 M 小时」。顶栏和推送里说同一句话。"""
    if mins < 60:
        return f"{mins} 分钟"
    h, m = divmod(mins, 60)
    if h < 24:
        return f"{h} 小时 {m} 分" if m else f"{h} 小时"
    d, h = divmod(h, 24)
    return f"{d} 天 {h} 小时" if h else f"{d} 天"


def notify(message: str, **kw) -> None:
    """弹通知。带 type 的一律改深色前景。

    Quasar 给 positive/warning/info 配的底色都是亮的（warning #F2C037、positive #21BA45），
    而它写死配白字 —— 实测只有 1.9~2.9:1，看不清。negative 也一样：上面 ui.colors 把它
    定成了 red-400，同样是亮底。不带 type 的通知【不能】动，那时底色是 Quasar 默认的
    #323232 深灰，改成深字反而只剩 1.6:1。

    【必须写驼峰 textColor】NiceGUI 只把 close_button/multi_line 转驼峰（见 ARG_MAP），
    其余原样下发给 Quasar；写成 text_color 会被它当不认识的选项静默忽略 ——
    看起来改了、实际什么都没发生。
    """
    if kw.get("type") in ("positive", "negative", "warning", "info"):
        kw.setdefault("textColor", "dark")
    try:
        ui.notify(message, **kw)
    except RuntimeError:
        # 【异步动作跑完时，发起它的那个按钮已经不在了】「立即跑一轮」要跑几分钟，
        # 中途别的动作 refresh 了那一页，按钮所在的容器被删，回来再 notify 就抛
        # "parent element … deleted"（日志里真出现过两次）。抛出去的后果是这句提示
        # 和它后面的 *_view.refresh() 一起丢掉 —— 页面停在旧数据上、还没有任何提示。
        # 各个异步动作里已经用 with client: 兜住了大多数情况，这里是最后一道。
        log.info("提示没地方弹了（页面已重建）：%s", message)


def item_url(source: str, item_id: str) -> str:
    """商品页地址由各数据源自己给 —— 路径规则各家不一样，别在这里拼。"""
    src = sources.get(source)
    return src.item_url(item_id) if src else "#"


def source_name(key: str) -> str:
    src = sources.get(key)
    return src.name if src else key


def deal_line(rule: dict, median) -> str:
    """这条规则【当前生效】的捡漏线，一句话。算不出就空串。
    手动价一填就盖过百分比 —— 和 core/matcher.is_deal 同一个口径，面板各处都用这一个函数。"""
    if rule.get("deal_price"):
        return f"手动捡漏价 {yen(rule['deal_price'])}"
    if median and rule.get("deal_ratio"):
        return f"低于 {yen(median * rule['deal_ratio'] // 100)} 算捡漏"
    return ""


def budget_text(rule: dict) -> str:
    """价格区间。上限 0 是「不限」，不是 ¥0 —— 「预算 ¥500,000〜¥0」看着像配错了。"""
    lo, hi = rule.get("price_min") or 0, rule.get("price_max") or 0
    if hi:
        return f"{yen(lo)}〜{yen(hi)}"
    return f"{yen(lo)} 起，不限上限" if lo else "不限"


def yen(n) -> str:
    return f"¥{n:,}" if n is not None else "—"


def freshness(r: dict, hours: int, cold_start: bool) -> str:
    """返回 "listed"（新上架）/ "found"（新发现）/ ""（都不是）。

    【为什么要分两个】原先只有一个「新」标，判的是 first_seen_at —— 也就是
    「我们什么时候抓到的」，而不是「商品什么时候挂出来的」。实测这两个差得离谱：
    Mercari 的商品平均滞后 185 天，最久的一件上架 1405 天后我们才第一次抓到它。
    于是一件挂了三年多的商品会被标成「新」，标签完全没有信息量。

      新上架  商品本身刚挂出来（用平台给的 listed_at）—— 你直觉理解的「新」
      新发现  商品早就挂着，但今天才进我们的库。多半是它降价进了你的价格区间，
              对你来说同样是新机会，只是性质不同，不该和上面混为一谈

    ヤフオク 的搜索结果不给上架时间，它的商品只可能是「新发现」。
    """
    win = timedelta(hours=hours)
    now = config.now()

    # 【新上架】判的是平台给的上架时间，和我们什么时候开始监控毫无关系 ——
    # 所以冷启动【不】抑制它。之前把它也一起抑制了，结果规则刚建起来的头两天
    # （fresh_hours 调到 48 就是两天）整页一个徽标都看不到，正是最想看的时候。
    if r["listed_at"] and now - r["listed_at"] < win:
        return "listed"

    # 【新发现】判的是"我们刚抓到"。规则刚开始监控时库里所有东西都是刚抓到的，
    # 这个标会糊满整页，那才是真的没有信息量 —— 只抑制它。
    if cold_start:
        return ""
    if now - r["first_seen_at"] < win:
        return "found"
    return ""


def auction_note(r: dict) -> tuple[str, str]:
    """拍卖商品的提示文字和颜色。返回 ("", "") 表示这不是拍卖。

    【为什么必须显示】判定用的是【当前价】——而拍卖的当前价只在此刻成立。
    一件已有 10 次出价的卡，当前价落在你预算内不代表你买得到。
    不把出价数和剩余时间摆出来，"命中"两个字就是在误导人。
    """
    if r.get("bid_count") is None:
        return "", ""
    bids = r["bid_count"]
    # 【结束了就换一套说法】追踪页现在会留着已成交的商品，那一行原先照旧写
    # "竞价中 · 当前价还会涨 · 已结束" —— 自己跟自己打架。结束之后
    # 唯一还有意义的是"最后被多少人抬到这个价"，那是下次出价的参考。
    if r.get("status") in ("sold_out", "gone"):
        if bids > 0:
            return f"🔨 拍卖结束 · 共 {bids} 次出价，这是最终成交价", "text-gray-400"
        return "🔨 拍卖结束 · 无人出价", "text-gray-400"
    left = config.time_left(r.get("end_time"))
    urgent = r.get("end_time") and (r["end_time"] - config.now()).total_seconds() < 3600
    if bids > 0:
        return f"🔨 竞价中 · 已 {bids} 次出价 · 当前价还会涨 · {left}", \
               ("text-red-400" if urgent else "text-orange-400")
    buy = r.get("buy_now_price")
    tail = f" · 一口价 {yen(buy)}" if buy else " · 无一口价，只能竞价"
    return f"🔨 拍卖 · 暂无人出价{tail} · {left}", \
           ("text-red-400" if urgent else "text-gray-400")


# 【哪几页的数据已经过时，切过去时再重建】不是当场全刷。
# 原先每个动作都把相关的几页当场重建一遍，实测点一次「标记」要跑 1.24 秒的 SQL
# （命中 697ms + 成交 344ms + 追踪 137ms + 标记 64ms），而你看得见的变化只是
# 一面旗换了颜色 —— 那面旗现在由 corner_toggle 就地翻。
# 别的页反正你没在看：等你真切过去，那一下的重建藏在切页动作里，看不出来。
_DIRTY: set[str] = set()


def stale_tabs(*names: str) -> None:
    """把这几页标成"数据过时了"，切过去的时候再重建。"""
    _DIRTY.update(names)


def toggle_track(source: str, item_id: str, rule_id: int, on: bool) -> None:
    """加入/取消追踪。追踪中的商品会被单独拉详情刷新，比整轮扫描快得多。

    【追踪页必须当场刷，别的页不用】在追踪页点掉一颗星，那一行就得当场消失 ——
    不消失的话你会以为没点上。命中页那颗星已经就地翻过去了，
    这一页别的东西和这次点击没有任何关系。
    """
    store.set_tracked(source, item_id, rule_id, on)
    n = len(store.tracked_items())
    if on:
        st = store.get_settings()
        # 【必须把代价说清楚】这是全项目唯一按件发请求的功能，追得多会烧穿配额，
        # 而配额一满是所有规则所有源一起停。让人在加的那一刻就看到数字。
        notify(f"已加入追踪（共 {n} 件）。每 {st['track_min']} 分钟单独刷新一次，"
               f"每轮最多 {st['track_budget']} 件 —— 追得越多，每件实际间隔越长")
    else:
        notify(f"已取消追踪（还剩 {n} 件）")
    track_view.refresh()


def toggle_mark(row: dict, rule_name: str, on: bool):
    """标记/取消标记。和追踪的区别：这个【一个请求都不发】，随便标。

    【有备注的不许一下点没】取消标记＝删那一行，备注跟着没了。旗子就在图角上，
    误点一下几个月前写的那句"为什么标它"就消失了。有备注的先拒绝并把旗翻回去
    （返回 False），要删就先把备注清空 —— 多一步，但那一步是有意识的。

    【只有标记页当场刷】在标记页点掉一面旗，那一行得当场消失。
    追踪页和成交页上这件商品也画着旗，但你此刻没在看它们 —— 标成过时，
    切过去时再重建。命中页那面旗已经就地翻过去了，不用重建。
    """
    if not on:
        note = row.get("note")
        if note is None:                                # 命中页的行没有 note 列，查一下
            note = store.mark_note_of(row["source"], row["item_id"])
        if note:
            notify("这条标记有备注，没有直接删：先到「标记」页把备注清空再取消（防误点）",
                   type="warning")
            return False
    store.set_marked(row, rule_name, on)
    n = len(store.marked_ids())
    notify(f"已标记（共 {n} 件），在「标记」页看" if on else f"已取消标记（还剩 {n} 件）")
    marks_view.refresh()
    stale_tabs("追踪", "成交")


def toggle_hide(row: dict, rule_name: str, on: bool) -> None:
    """从命中页剔除 / 恢复这一件。

    【只是不显示，不是删除也不是拉黑】三件事经常被混为一谈，代价差很远：
      剔除    命中页不再显示它、也不再推送。照常抓取、照常对账
      拉黑卖家 【所有规则、所有源】下这个卖家的商品立刻判为不合适，也不再推送
      删规则   连商品带成交样本一起删掉
    所以提示里必须把"它还在"说清楚，否则你会以为自己刚刚扔掉了什么。
    """
    store.set_hidden(row, rule_name, on)
    if on:
        notify(f"已剔除「{row['name'][:20]}」—— 命中页不再显示、也不再推送，"
               "商品照常抓取，全部页照常看得到。要拿回来：命中页最下面「已剔除」")
    else:
        notify("已恢复，它会重新出现在命中页")
    hits_view.refresh()


def save_mark_note(source: str, item_id: str, value) -> None:
    store.set_mark_note(source, item_id, (value or "").strip())
    notify("备注已存")
    marks_view.refresh()


def corner_toggle(on: bool, icons: tuple[str, str], tips: tuple[str, str],
                  base: str, on_cls: str, act, twins: dict | None, key) -> None:
    """图片角上的一个开关（追踪星 / 标记旗）。点一下【就地】翻面，不重建整页。

    【为什么非就地不可】原先每次点击都把相关的几页从头重建。实测点一次「标记」
    要跑 1.24 秒的 SQL —— 命中页 697ms（19 条查询）+ 成交页 344ms + 追踪页 137ms
    + 标记页 64ms，外加上千个页面元素重新下发。而你看得见的变化只是一面旗
    换了个颜色。一屏几十行，等这一下的工夫你会以为没点上、再点一次，
    第二次点的是已经翻过去的状态 —— 等于又翻回来，而且你不知道。

    【先画后写】画在落库之前。这一步就是为了"马上生效"，而落库要走一个
    数据库往返。真写失败的话图标会停在错的那一面，但下一次任何刷新都会纠正 ——
    比让你对着一个没反应的按钮猜要好。

    【twins：同一件商品在一页上会出现两次】捡漏汇总里一次、它所在的规则组里
    再一次。点了其中一个，另一个必须跟着翻，否则同一屏上同一件东西一颗星
    实心一颗空心，你会以为自己点漏了。同一个 key 共用一份状态和一组重画函数。
    """
    box = twins.setdefault(key, {"on": on}) if twins is not None else {"on": on}
    box.setdefault("draw", [])

    def flip() -> None:
        box["on"] = not box["on"]
        for d in box["draw"]:
            d()
        if act(box["on"]) is False:         # 落库那边拒绝了（比如有备注的标记）：翻回去
            box["on"] = not box["on"]
            for d in box["draw"]:
                d()

    btn = ui.button(icons[box["on"]], on_click=lambda _: flip()).props(BTN_CORNER)
    with btn:
        tip = ui.tooltip(tips[box["on"]])

    def draw() -> None:
        btn.text = icons[box["on"]]
        btn.classes(replace=base + (" " + on_cls if box["on"] else ""))
        tip.text = tips[box["on"]]

    box["draw"].append(draw)
    draw()


def thumb_corners(r: dict, rule_id: int, marks: set, rule_name: str,
                  star: bool = True, twins: dict | None = None) -> None:
    """来源标签 + 缩略图 + 两个角标：右上角的追踪星，右下角的标记旗。

    【来源标签压在图上方】它原先在右边价格列的顶上。价格列后来多了「上次 / 首见」
    两行，左右两列高度对不上了 —— 图那一列矮一截，整行看着往上飘。
    把标签挪到图上方正好补回那一行，不用把缩略图整个放大（放大会让一屏
    装不下几件，而这一页的价值就是一眼扫完）。
    标签本来就描述"这件东西在哪买"，和图、和标题是一组，本来就不该在价格那边。

    【两个角标是两回事，别合并】
      ★ 追踪：会真的花请求去单独刷新它，所以有件数上限、要克制。
      ⚑ 标记：一个书签，不发任何请求，随便标。
    放在同一张图的不同角上，是因为它们针对的是同一件商品、又都属于"我对它的态度"；
    但颜色和图形必须分开（琥珀星 / 青旗），否则点错了代价完全不同。

    star=False 给成交页和标记页用：那里的商品已经卖掉了，追踪它没有意义
    （不会再刷新），但把成交价记下来很有意义。

    【占位框不能省】某个源哪天返回空缩略图，没有占位的话这一行的正文会直接顶到
    最左边，和上下几行错开 —— 而且角标也没地方挂。
    """
    tracked = r.get("tracked_at") is not None
    marked = (r["source"], r["item_id"]) in marks
    key = (r["source"], r["item_id"])
    with ui.element("div").classes("relative shrink-0 w-24 h-24"):
        if r["thumb_url"]:
            # 【ratio=1 不能省】q-img 的高度是按图片真实宽高比撑出来的，
            # Tailwind 的 h-24 管不住它（object-cover 是给原生 <img> 的，
            # 这里外层是 Quasar 组件）。不写 ratio 的话一列里 96px 高和
            # 44px 高的缩略图混着排，每行正文的起点都不在一条线上。
            ui.image(r["thumb_url"]).props("fit=cover ratio=1") \
                .classes("w-24 h-24 rounded")
        else:
            ui.element("div").classes("w-24 h-24 rounded bg-white/5")
        if star:
            corner_toggle(
                tracked, ("☆", "★"),
                ("加入追踪：这件会被单独拉详情刷新，价格、出价数、是否卖掉都比"
                 "整轮扫描快得多。代价是每次刷新一个请求", "取消追踪"),
                "absolute top-0 right-0 star-btn", "star-on",
                lambda on, so=r["source"], ii=r["item_id"], ri=rule_id:
                    toggle_track(so, ii, ri, on),
                twins, ("star",) + key)
        corner_toggle(
            marked, ("⚐", "⚑"),
            ("标记：只是记一笔，不发任何请求。标的是【此刻的快照】——"
             "标题、价格、图都存下来，以后商品下架了这一页照样看得到", "取消标记"),
            "absolute bottom-0 right-0 star-btn mark-btn", "mark-on",
            lambda on, row=dict(r), rn=rule_name: toggle_mark(row, rn, on),
            twins, ("mark",) + key)


def stale_hours(r: dict) -> float:
    """这件商品有多久没在搜索结果里出现过了（小时）。

    【为什么用 last_seen_at 而不是别的】它的语义就是「最后一次拿到这件商品的新数据」，
    整轮扫描和单独拉详情都会刷新它。健康情况下每轮都会重新看到在售商品
    （quick_min，7〜30 分钟），所以这个数一旦涨到几小时，一定是出事了 ——
    多半是那个源在限流（实测 メルカリ 会回 403），对账拉不到详情，
    判不出它到底卖掉了还是还挂着，于是它就带着几小时前的旧价格一直挂在命中页上。
    """
    return (config.now() - r["last_seen_at"]).total_seconds() / 3600


def split_stale(rows: list[dict], hide_h: float) -> tuple[list[dict], list[dict]]:
    """按「多久没见到」把命中列表切成（还能信的，已经撤下的）。

    【拆成独立函数是为了能离线测】这条界线上两种写反的方向后果都很难发现：
    切多了会静悄悄少几件（你以为这个价位真的没货），切少了等于没做
    （旧价格照常冒充在售）。而这两种都不会报错、不会进日志。
    """
    keep = [r for r in rows if stale_hours(r) <= hide_h]
    dark = [r for r in rows if stale_hours(r) > hide_h]
    return keep, dark


def price_history(r: dict, prev) -> None:
    """价格列里「上次 / 首见」那两行小字。prev 是 store.prev_prices 里的那个二元组。

    【命中页和追踪页共用一个函数】两边的价格列本来就是同一套版式，各写一遍的话
    改了去重规则只改一处，而两处长得一样，看不出来对不上。

    【首见和上次相同时只写一行】只变过一次价的商品这两个数是同一个，
    重复写出来反而让人以为中间还漏了一档没显示。
    【一次都没变过价的什么都不写】首见就是现价，再写一行「首见 ¥X」等于
    把同一个数说两遍，还占掉一行高度 —— 库里 646 行里有 550 行是这种。
    """
    seen = {r["price"]}
    lines = []
    if prev and prev[0] not in seen:
        seen.add(prev[0])
        lines.append(("上次", prev[0], prev[1], "变成现在这个价"))
    if r.get("first_price") and r["first_price"] not in seen:
        lines.append(("首见", r["first_price"], r.get("first_seen_at"), "第一次看到它"))
    for label, price, at, what in lines:
        el = ui.label(f"{label} {yen(price)}").classes("text-xs text-gray-500")
        if at:
            el.tooltip(f"{at:%m-%d %H:%M} {what}")


def drop_hidden(rows: list[dict], hidden: set) -> list[dict]:
    """把手动剔除的从命中列表里去掉。

    【必须排在 split_stale 前面】反过来的话，被剔除的商品会先被算进
    「另有 N 件超过 24h 没见到，已撤下」那句里 —— 那句话就开始说谎，
    而它恰恰是用来让人相信"没有东西被偷偷藏起来"的。
    """
    return [r for r in rows if (r["source"], r["item_id"]) not in hidden]


def blacklist_seller(seller_id: str, row: dict | None = None) -> None:
    """把这个卖家加进【全局】拉黑列表，并立刻重判所有规则。

    【为什么是全局】同一个刷屏的店铺会同时撞上好几条规则，按规则各拉一次等于
    同一个意图要表达 N 遍，而且漏掉一条，他的商品照样从那条规则推到你手机上。

    【为什么要立刻重判、而且是全部规则】这是个手动动作：点完按钮商品还挂在
    页面上，人会以为没生效然后再点一次。只重判当前这条的话，别的规则下那批
    商品在改判之前就已经被 finalize 推出去了。代价见 poller.revalidate_all。
    """
    sid = (seller_id or "").strip()
    if not sid:
        notify("这件商品没有卖家ID，没有可拉黑的对象", type="warning")
        return
    if sid.lower() in store.blocked_sellers():
        notify(f"{sid} 已经在拉黑列表里了")
        return
    store.block_seller(sid, row)
    # 【必须写完库再重判】block_seller 末尾才让缓存失效；顺序反了就是拿旧列表
    # 重判，结果是「已拉黑」却一件都没改判，而商品原地不动 —— 人只会再点一次。
    poller.revalidate_all()
    notify(f"已拉黑 {sid}：他名下 {store.seller_item_count(sid)} 件商品在所有规则下"
           "都判为不合适，也不再推送。要反悔：「规则」页最下面的「拉黑列表」")
    _refresh_after_block()


def unblock_seller(seller_id: str) -> None:
    """解除拉黑。被误杀的商品下一次重判就回到命中页 —— 这正是拉黑只判
    matched=0 而【仍然入库】的理由。"""
    store.unblock_seller(seller_id)
    poller.revalidate_all()
    notify(f"已解除 {seller_id}，他名下 {store.seller_item_count(seller_id)} 件商品已重判"
           "（合适的回到命中页）")
    _refresh_after_block()


# 卖家ID 的列宽（blocked_seller.seller_id / item.seller_id 都是 VARCHAR(32)）
SELLER_ID_MAX = 32


def add_blocked(inp) -> None:
    """手动添加。一次可以粘多个 —— 半角/全角逗号、顿号、分号、换行都认。

    【这个入口不能省】拉黑按钮只在有卖家ID的行上有，而你可能是在源站页面上
    先看到这个人的。规则对话框里原本那个 exclude_sellers 文本框没了，手输的
    路子必须在这里补回来。

    【原样入库，只拿小写去重】用 id_tokens 而不是 ids：ids() 的小写是【比对】
    口径，拿它的结果直接入库会把 ヤフオク / メルカリShops 那些大小写敏感的
    base62 压成小写。判定照常（两边都小写），但列表里显示的那串就再也复制
    不回源站打开了 —— 而手动加的那行没有商品链接，那串ID是它唯一的把手。

    【不像 ID 的先拦下来出声】提示语写着"卖家ID"，人照样会顺手粘一整条
    卖家主页 URL。它比列宽长，落库时会被截成半截、拦不住任何商品，而下次
    再粘同一条时"已在列表里"又比不中（库里是截断后的），于是每次都提示
    「已拉黑 1 人」、每次都白跑一遍全局重判，永远收敛不了，日志里毫无线索。
    """
    have = store.blocked_sellers()
    fresh, dup, bad = [], 0, []
    for raw in id_tokens(inp.value):
        if len(raw) > SELLER_ID_MAX or "/" in raw or ":" in raw:
            bad.append(raw)
        elif raw.lower() in have or raw.lower() in {x.lower() for x in fresh}:
            dup += 1
        else:
            fresh.append(raw)
    if bad:
        notify(f"这些不像卖家ID（超过 {SELLER_ID_MAX} 个字符，或者是网址）：" +
               "、".join(b[:40] for b in bad[:3]) +
               "。只粘ID那一段，别粘整条链接", type="negative")
        return
    if not fresh:
        notify("没有新的 ID（空的，或者都已经在列表里了）", type="warning")
        return
    for sid in fresh:
        store.block_seller(sid)
    # 输入框不用手动清空：下面的 rules_view.refresh() 会把这一块整个重建。
    poller.revalidate_all()
    notify(f"已拉黑 {len(fresh)} 人" + (f"（另有 {dup} 个已经在列表里）" if dup else "") +
           "，全部规则已重判")
    _refresh_after_block()


def _refresh_after_block() -> None:
    # 三个页都要刷：动作可能从命中页或「全部」页发起，而列表在规则页。
    # 【不要改写成 stale_tabs("规则")】REFRESH 里只登记了命中/追踪/标记/成交
    # 四页，切过去时 REFRESH[name].refresh() 会 KeyError。
    # refresh() 不带参数会沿用各自最近一次的参数（NiceGUI 的
    # target.args = args or target.args），所以「全部」页的筛选条件不会被重置回
    # 「全部规则」。没渲染过的页 targets 为空，是空操作。
    hits_view.refresh()
    all_view.refresh()
    rules_view.refresh()


@contextmanager
def toolbar(desc: str):
    """每一页顶上那一行：左边操作按钮，右边一句话说明。

    统一它是因为原先五个页各长各的 —— 有的有说明有的没有，成交页的说明跟在
    按钮后面、设置页的塞在正文第一行，字号和间距也各不相同。
    页面一多，这种参差比任何单点的丑都更显得没收拾过。
    """
    # 【rw-toolbar 是给 CSS 定位用的】参考端的 .icon-btn 有底色有描边，
    # 但那个质感只该给页首这一行的按钮 —— 行内那些「剔除 / 拉黑卖家」是
    # 刻意做轻的，套上按钮框会把整页压满方块。作用域靠这个类圈出来。
    with ui.row().classes("rw-toolbar items-center gap-2 mb-3 flex-wrap w-full"):
        yield
        # 参考端的 .count（app.css L75）：13px、muted 色，顶栏底下那行小灰字
        ui.label(desc).classes("rw-desc")


# ------------------------------------------------------------------ 命中页

def _deals_summary(rules: list[dict], live: list[dict], cold: dict, marks: set, hidden: set,
                   prevs: dict, twins: dict,
                   fresh_hours: int, warn_h: float, hide_h: float) -> None:
    """跨规则的捡漏汇总 —— 命中页上唯一默认展开的块。

    【为什么要单开一块，而不是靠各规则组里的绿徽标】捡漏是「现在该动手」的那几件，
    平时一天 0〜4 件；而命中是「符合条件」的，四条规则加起来上百件。
    混在一起的结果是：真正要抢的东西埋在几十屏里，你得逐组展开去找绿标。
    分开之后这一页的默认视图就只剩「要不要买」这一个问题。

    【口径必须和规则组完全一致】同一套 _hit_row、同一个 split_stale 撤下规则。
    两边显示的件数对不上是最难查的那种 bug —— 你会以为是判定出错了。
    """
    by_id = {r["id"]: r for r in rules}
    # 【跨规则重排】live 是按规则分块的；这里要的是"全站最便宜的排最前"
    rows = sorted((r for r in live if r["is_deal"] and r["rule_id"] in by_id),
                  key=lambda r: (r["deal_pct"] if r["deal_pct"] is not None else 999, r["price"]))
    # 【剔除要排在撤下前面】理由见 drop_hidden
    kept = drop_hidden(rows, hidden)
    n_hidden, rows = len(rows) - len(kept), kept
    rows, dark = split_stale(rows, hide_h)

    head = f"🟢 捡漏　{len(rows)} 件" if rows else "🟢 捡漏　暂时没有"
    cap = ["低于各自规则捡漏线的在售商品，按「市价的百分之多少」从低到高排"]
    if dark:
        cap.append(f"另有 {len(dark)} 件超过 {hide_h}h 没见到，已撤下")
    if n_hidden:
        # 【剔掉的也要报数】这一块回答的是「现在有什么值得抢」，
        # 少几件而不说，你会以为今天真的就这么点货
        cap.append(f"另有 {n_hidden} 件被你剔除")
    with ui.expansion(head, caption=" · ".join(cap), value=True) \
            .classes(CARD).props("header-class=text-base"):
        if not rows:
            # 【说清楚是"没有"还是"判不了"】捡漏线算不出来的规则（没手动价、
            # 成交样本又不够）永远不会有捡漏，而页面上看起来和"暂时没好货"一模一样。
            blind = [r["name"] for r in rules
                     if not r["deal_price"]
                     and not (store.get_state(r["id"])["median_price"] and r["deal_ratio"])]
            msg = "当前没有低于捡漏线的商品。"
            if blind:
                msg += ("　⚠ 另外这几条规则【算不出捡漏线】，永远不会出现在这里："
                        + "、".join(blind) + " —— 去下面的规则组里填个手动捡漏价")
            ui.label(msg).classes("text-gray-400 text-sm")
            return
        with ui.element("div").classes("grid grid-cols-1 xl:grid-cols-2 gap-x-6 w-full"):
            for r in rows:
                rule = by_id[r["rule_id"]]
                _hit_row(r, rule, marks, prevs, twins, fresh_hours, cold[rule["id"]],
                         warn_h, hide_h, show_rule=True)



def _load_hits() -> dict:
    """命中页要的数据，8 条 SQL 取完（原先 19 条：每条规则 3 条 + 汇总单独再查一遍）。

    live 是全部在售命中，按规则分块、块内按市价百分比升序；汇总区从它里面挑
    is_deal 的再【跨规则】重排 —— 直接按分块顺序过滤的话最便宜的那件不会排最前。
    """
    live = store.live_matched()
    return {"rules": store.get_rules(), "marks": store.marked_ids(),
            "hidden": store.hidden_ids(), "live": live,
            "prevs": store.prev_prices(live), "first_seen": store.first_seen_by_rule(),
            "states": store.rule_states()}


# 【记住哪几个规则组是展开的】hits_view.refresh() 会把整页重建，expansion 是新元素，
# 只认构造参数 —— 原先剔除/拉黑/存捡漏价之后，你刚展开的组全部折回去、滚动位置也丢了，
# 存个捡漏价当前组立刻合上。界面状态，不是阈值，和 _DIRTY 一个路子。
_OPEN: dict[int, bool] = {}


@ui.refreshable
def hits_view(host=None) -> None:
    # 【整份取出来，不要每行查一次】一屏几十行，每行一个 SELECT 翻页会肉眼卡顿
    d = _load_hits()
    marks, hidden, prevs, rules = d["marks"], d["hidden"], d["prevs"], d["rules"]
    # 同一件捡漏商品在这一页上会出现两次（汇总里一次、规则组里一次），
    # 角标要一起翻。这份表跟着本次重建走，重建一次就是新的一份，不会留下
    # 指向已删元素的重画函数。
    twins: dict = {}
    if not rules:
        ui.label("还没有规则，去「规则」页新建一条。").classes("text-gray-400 p-4")
        return

    cfg = store.get_settings()
    fresh_hours = cfg["fresh_hours"]
    warn_h, hide_h = cfg["stale_warn_hours"], cfg["stale_hide_hours"]
    # 冷启动判断：这条规则库里【最早】的商品也是刚抓到的，说明监控本身才刚开始。
    cold = {}
    for rule in rules:
        earliest = d["first_seen"].get(rule["id"])
        cold[rule["id"]] = bool(earliest) and (config.now() - earliest) < timedelta(hours=fresh_hours)

    _deals_summary(rules, d["live"], cold, marks, hidden, prevs, twins, fresh_hours, warn_h, hide_h)

    for rule in rules:
        st = d["states"].get(rule["id"]) or store.STATE_DEFAULT
        med = st["median_price"]
        cold_start = cold[rule["id"]]
        rows = [r for r in d["live"] if r["rule_id"] == rule["id"]]
        # 【太久没见到的从这一页撤下】命中页回答的是「现在有什么能买」。一条一天
        # 没更新过的记录回答不了这个问题。撤下不是删除：全部页照常看得到，
        # 只要它重新出现在搜索结果里，last_seen_at 一刷新就自动回来。
        # 【剔除要排在撤下前面】理由见 drop_hidden
        kept = drop_hidden(rows, hidden)
        n_hidden, rows = len(rows) - len(kept), kept
        rows, gone_dark = split_stale(rows, hide_h)

        # 折叠摘要：折起来之后这一行就是你能看到的全部，所以预算/市价/捡漏线都要在里面
        summary = [f"预算 {budget_text(rule)}"]
        if med:
            summary.append(f"市价中位 {yen(med)}（{st['sample_count']}件成交）")
        else:
            summary.append(f"成交样本不足{rule['median_min_samples']}件，暂无市价参考")
        # 【手动价填了就盖过百分比】摘要必须如实反映当前生效的那一个，
        # 否则你会对着「低于 ¥706,500 算捡漏」纳闷为什么 ¥70 万的没标捡漏。
        line = deal_line(rule, med)
        if line:
            summary.append(line)
        if gone_dark:
            # 【必须报数】偷偷少几件比显示旧数据更糟：你会以为这个价位真的没货了
            summary.append(f"另有 {len(gone_dark)} 件超过 {hide_h}h 没见到，已撤下")
        if n_hidden:
            # 同上：剔除是你自己按的，但过几天你不会记得按过几件
            summary.append(f"另有 {n_hidden} 件被你剔除")
        deals = sum(1 for r in rows if r["is_deal"])
        head = f"{rule['name']}　{len(rows)} 件" + (f"　🟢 {deals} 件捡漏" if deals else "")

        # 【默认折叠】整页四条规则铺开有上百件，一进来就得滚半天才看得到重点。
        # 真正要立刻动手的那几件已经在最上面的「捡漏」汇总里，
        # 这些按规则分的组是「我想翻一翻某一类」时才展开的。
        rid = rule["id"]
        # 【折叠着的组不建行】一页 80 行 × 每行 23 个元素，其中 1500 个藏在默认折叠的组里，
        # 每次重建都白建、白下发（实测浏览器主线程每次多阻塞 0.5 秒）。
        # 展开那一刻再建；已经展开过的组（_OPEN）重建时当场建。
        with ui.expansion(head, caption=" · ".join(summary), value=_OPEN.get(rid, False)) \
                .classes(CARD).props("header-class=text-base") as group:
            # 【放在「没有商品」判断之前】一件都没命中的时候，恰恰是你最想调这个价的时候。
            # 原先这一行是五个元素平铺：按钮、标签、输入框、按钮、一长串说明 ——
            # 输入框比周围都高、两个按钮同色同权重、说明拖着很长的尾巴，像堆在一起的。
            # 改成：左边一个带 ¥ 前缀和内嵌保存键的紧凑输入框，右边一个次要入口，
            # 说明收进 tooltip（要看的人 hover 一下就有，不看的人不用被它占一行）。
            with ui.row().classes("items-center gap-3 mb-3 flex-wrap"):
                # 【必须用默认参数绑死 rid/comp】这两个是循环变量，直接引用的话
                # 等你点保存时它们早就指向最后一条规则了 —— 每个按钮都会改同一条。
                inp = ui.number(value=rule["deal_price"] or None, format="%d",
                                placeholder="不填＝按市价百分比") \
                    .props(INPUT + ' prefix="捡漏价 ¥" input-class=text-right') \
                    .classes("w-72").tooltip(
                        "低于这个价就算捡漏。填了就完全盖过「成交中位数 × 百分比」那套，"
                        "而且不依赖成交样本 —— 样本不够、或挂单价整体高于成交价时，"
                        "百分比那条线永远碰不到，只能用它。留空或 0 = 回到按百分比判")
                # 【保存键放进输入框内部】原先它是外面一个 size=sm 的按钮，只有输入框
                # 一半高（24px vs 40px），并排放着高度差一眼就看出来。塞进 append 插槽
                # 之后高度由 q-input 自己保证，不用去凑两个控件的尺寸。
                with inp.add_slot("append"):
                    ui.button("保存", on_click=lambda _, rid=rule["id"], c=inp:
                              save_deal_price(rid, c.value)) \
                        .props(BTN_GHOST).classes("px-2")
                # 【host 必须一路传进来】对话框要建在页面级容器里，不能建在
                # hits_view 自己的刷新容器内 —— refresh() 的第一步是 container.clear()，
                # 会把还开着的对话框连同你敲了一半的内容一起删掉，而且不给任何提示。
                # 【降到灰色】它和框里那个「保存」挨着，两个都用主色的话权重一样，
                # 而一个是提交这条价、一个是打开整条规则的设置框，职责差很远。
                ui.button("设置规则", on_click=lambda _, r=rule: rule_dialog(r, host)) \
                    .props(BTN_QUIET) \
                    .tooltip("改关键词、词表、价格区间、数据源 —— 和「规则」页是同一个框")
            if not rows:
                ui.label("当前没有符合条件的在售商品。").classes("text-gray-400 text-sm")
                continue

            # 【宽屏两列】一件商品那一行最窄要 ~480px 才不挤（图 96 + 正文 + 价格列），
            # xl 是 80rem=1280px，两列各约 610px，够。再窄就退回一列 ——
            # 不设下限的话，笔记本上正文会被压到标题每行只剩几个字。
            # gap 只给 x 方向：纵向的间距由每行自己的 border-t + py-2 负责，
            # 再加 gap-y 会让两列之间的分隔线对不齐。
            holder = ui.element("div").classes("grid grid-cols-1 xl:grid-cols-2 gap-x-6 w-full")
            built = {"done": False}

            def build(rows=rows, rule=rule, cold_start=cold_start, holder=holder, built=built):
                if built["done"]:
                    return
                built["done"] = True
                with holder:
                    for r in rows:
                        _hit_row(r, rule, marks, prevs, twins, fresh_hours,
                                 cold_start, warn_h, hide_h)

            def on_toggle(e, rid=rid, build=build):
                _OPEN[rid] = bool(e.value)
                if e.value:
                    build()

            group.on_value_change(on_toggle)
            if _OPEN.get(rid):
                build()

    _hidden_block()


def _hidden_block() -> None:
    """页尾的「已剔除」——剔除唯一的退路。

    【没有这一块就不该做剔除这个功能】按错一下就再也找不回来，
    而且页面上少了一件你根本不会发现。放在命中页最下面而不是单开一个页签：
    它只影响这一页，看的人也只在这一页上发现"怎么少了一件"。
    """
    rows = store.hidden_items()
    if not rows:
        return
    with ui.expansion(f"已剔除　{len(rows)} 件",
                      caption="这些不在命中页显示、也不再推送；商品本身照常抓取；"
                              "点「恢复」就回来",
                      value=False).classes(CARD).props("header-class=text-base"):
        for m in rows:
            with ui.row().classes("items-center w-full gap-3 border-t py-2 sm:flex-nowrap"):
                ui.badge(source_name(m["source"])).classes(BADGE_LABEL)
                ui.link(m["name"], item_url(m["source"], m["item_id"]),
                        new_tab=True).classes("flex-1 min-w-0 break-words")
                if m["rule_name"]:
                    ui.label(m["rule_name"]).classes("text-xs text-gray-400 shrink-0")
                ui.label(f"{m['hidden_at']:%m-%d %H:%M} 剔除") \
                    .classes("text-xs text-gray-500 shrink-0")
                # 【绑死 row】m 是循环变量，理由同命中页那些按钮
                ui.button("恢复", on_click=lambda _, row=dict(m):
                          toggle_hide(row, "", False)) \
                    .props(BTN_GHOST).classes("shrink-0")


def _blocked_block() -> None:
    """规则页页尾的「拉黑列表」—— 拉黑唯一的退路，兼手动添加的入口。

    【和命中页那块「已剔除」不是一回事】剔除只管眼前这一条、只影响命中页；
    拉黑是全局的：所有规则、所有源，他的商品一律判不合适，也不再推送。

    【为什么放规则页而不是命中页】① 命中页那 8 条 SQL 正好顶在预算上，而这一块
    一周看不了一次；② 命中页「已剔除」那段注释明写"放这儿是因为它只影响这一页"，
    把一个全局控件挂在那条论证下面，下一个人会照着推出错的结论。
    动作入口在命中页和「全部」页，那两处的 tooltip 都指到这里来。

    【空列表也要画出来，不能像 _hidden_block 那样直接 return】它还兼着手动添加：
    空了就 return 的话，列表一旦删空就再也加不回去了。
    """
    rows = store.blocked_items()
    with ui.expansion(f"拉黑列表　{len(rows)} 人",
                      caption="对【所有规则、所有源】生效：他的商品一律判不合适、"
                              "也不再推送，但仍照常入库 —— 点「解除」下一次重判就回来",
                      value=False).classes(CARD).props("header-class=text-base"):
        with ui.row().classes("items-center w-full gap-2"):
            inp = ui.input(placeholder="卖家ID，可一次粘多个（逗号分隔）") \
                .classes("flex-1 min-w-0").props(INPUT)
            ui.button("添加", on_click=lambda: add_blocked(inp)).props(BTN_GHOST)
        for m in rows:
            with ui.row().classes("items-center w-full gap-3 border-t py-2 sm:flex-nowrap"):
                if m["source"]:
                    ui.badge(source_name(m["source"])).classes(BADGE_LABEL)
                ui.label(m["seller_id"]).classes("shrink-0")
                # 【要一个能点开的链接】光一串 22 位 base62，一周之后你根本认不出
                # 自己拉黑的是谁。存的是拉黑那一刻他挂的那件商品（快照）。
                if m["item_id"]:
                    ui.link(m["item_name"] or m["item_id"],
                            item_url(m["source"], m["item_id"]),
                            new_tab=True).classes("flex-1 min-w-0 break-words")
                else:
                    # 手动添加的没有商品可链接；迁移进来的那几条在 item_name 里
                    # 写着自己的来历，照样显示出来，不然这一行只剩一串数字。
                    ui.label(m["item_name"] or "（手动添加）") \
                        .classes("flex-1 min-w-0 break-words text-gray-400")
                ui.label(f"{m['blocked_at']:%m-%d %H:%M} 拉黑") \
                    .classes("text-xs text-gray-500 shrink-0")
                # 【绑死 sid】m 是循环变量，理由同上面
                ui.button("解除", on_click=lambda _, sid=m["seller_id"]:
                          unblock_seller(sid)) \
                    .props(BTN_GHOST).classes("shrink-0")


def _hit_row(r: dict, rule: dict, marks: set, prevs: dict, twins: dict,
             fresh_hours: int, cold_start: bool, warn_h: float, hide_h: float,
             show_rule: bool = False) -> None:
    """命中页的一行商品。

    【抽成函数是为了「捡漏汇总」能用同一套】汇总区和规则组里显示的是同一批商品，
    两边各写一遍的话迟早分叉：改了徽标顺序只改一处、加了新按钮只加一处，
    而两处长得差不多，评审时很难发现。
    """
    fresh = freshness(r, fresh_hours, cold_start)
    prev = prevs.get((r["source"], r["item_id"]))
    # 【正文列必须是 flex-1，不能是 grow】grow 只给 flex-grow:1，
    # flex-basis 还是 auto —— 而 flex 折行用的是「内容不折行时的完整
    # 宽度」(max-content)，min-w-0 只压下限、管不到折行这一步。
    # 日文标题动辄七八十字，max-content 接近 1000px，于是手机上正文列
    # 自己就撑爆一行：缩略图被单独留在第一行、右边一大片空白，标题掉到
    # 第二行、价格再掉第三行，一件商品从两行变三行、行高涨到 250px 上下。
    # flex-1 是 flex:1 1 0%，basis 归零后它在折行计算里占 0，
    # 图和正文才留得住在同一行，只把价格挤下去 —— 那才是本来想要的。
    # 【这一行的三个 class 是一组，缺一个价格就会被长标题挤下去】
    #   sm:flex-nowrap  ≥640px 时三列（图/正文/价格）绝不换行 ——
    #                没有它，标题一长整个价格列会被挤到下一行去。
    #   【但手机上必须换行】430px 宽时三列排不下，nowrap 的结果是
    #                价格列整个被推出视口：价格、市价百分比、拉黑
    #                全都看不见，而且没有横向滚动条提示你右边还有东西。
    #                宁可价格掉到第二行，也不能让它消失。
    #   items-start  标题换成两行时，价格保持在顶部对齐而不是浮到中间
    #   正文列的 min-w-0 + 价格列的 shrink-0 见下面，是同一件事的另一半：
    #   flex 子项默认 min-width:auto，不写 min-w-0 的话正文列会被内容
    #   撑到超过容器宽度，把右边挤没
    with ui.row().classes("items-start w-full gap-3 border-t py-2 sm:flex-nowrap"):
        # 96px：正文列在「徽标+标题两行+拍卖提示+品相行」时约 90px 高，
        # 图跟着长到差不多，两边才齐。64px 时右边明显空一块，
        # 看起来就像行距被撑开了。
        thumb_corners(r, rule["id"], marks, rule["name"], twins=twins)
        # leading-snug：正文是 3~4 行小字堆起来的，默认行高留白偏多，
        # 累积下来整张卡片会显得松垮
        # self-stretch：撑满卡片高度，下面那行小字的 mt-auto 才顶得到底
        with ui.column().classes(
                "gap-0 flex-1 min-w-0 leading-snug self-stretch"):
            # 【徽标在标题上方】它们是"要不要点进去"的信号，得一眼看见。
            # 放在标题后面的话，遇到长标题（全库最长 130 字，超 70 字的有
            # 一百多件）就会被推到第二三行的行尾，等于没有。
            # 没有任何徽标时整行不渲染，不留空档。
            dropped = r["price"] < r["first_price"]
            stale = stale_hours(r)
            if (r["is_deal"] or fresh or r["desc_warn"]
                    or r["ship_from"] == TOKYO or dropped
                    or stale > warn_h):
                with ui.row().classes("items-center gap-2 flex-wrap mb-1"):
                    # 【失联排在最前面】它一旦成立，下面那些
                    # 「捡漏」「已降」全都是基于旧价算的，
                    # 先看到它才知道后面几个徽标都不能全信。
                    if stale > warn_h:
                        # 【用灰不用橙】橙色是「新上架」，两个并排时
                        # 分不开，而它们语义正好相反：一个说"快看"，
                        # 一个说"别信"。失联和「描述未读到」是同一类
                        # ——数据质量提示，不是机会信号，归灰档。
                        ui.badge(f"失联 {stale:.0f}h", color="grey").tooltip(
                            f"已经 {stale:.0f} 小时没在搜索结果里见到它了"
                            f"（正常每 {rule['quick_min']} 分钟就该见到一次）。"
                            "多半是这个源在限流，对账拉不到详情，"
                            "判不出它是卖掉了还是还挂着 —— "
                            f"下面的价格是 {stale:.0f} 小时前的，别当真。"
                            f"超过 {hide_h}h 会直接从这一页撤下")
                    if r["is_deal"]:
                        # 【手动价判出来的要说清】它的「市价的 114%」就在右边，
                        # 不解释的话"捡漏"和"贵了一成"并排，像自相矛盾。
                        b = ui.badge("捡漏", color="green")
                        if rule.get("deal_price"):
                            b.tooltip(f"低于你填的手动捡漏价 {yen(rule['deal_price'])}"
                                      "（手动价一填就盖过市价百分比那条线）")
                    if show_rule:
                        # 【汇总区必须标规则名】它把四条规则的商品混在一起排，
                        # 不标的话「¥50万算捡漏吗」这个问题没法回答 ——
                        # 4090 单卡的线是 40 万，5090 整机的线是 110 万。
                        ui.label(rule.get("name", "?")).classes("text-xs text-gray-400")
                    if dropped:
                        # 【降价属于徽标不属于小字】它和捡漏/新上架是同一类
                        # 信息：决定你要不要点进去。摆在最下面那行灰字里，
                        # 和品相、发货地混在一起，等于把它藏了。
                        # 首见价放进 tooltip —— 徽标要短，一眼能扫过去。
                        # 【三个价都写进来】"已降 ¥18,765"只说了跌了多少，
                        # 没说是一次跌下来的还是一路阴跌 —— 而那两种的
                        # 后续走势完全不同。中间这一档就是用来区分的。
                        tip = f"首次发现时 {yen(r['first_price'])}"
                        if prev and prev[0] not in (r["price"], r["first_price"]):
                            tip += f"　→　上次 {yen(prev[0])}"
                        tip += f"　→　现在 {yen(r['price'])}"
                        ui.badge(f"已降 {yen(r['first_price'] - r['price'])}",
                                 color="red").tooltip(tip)
                    if r["ship_from"] == TOKYO:
                        ui.badge(TOKYO, color="purple").tooltip(
                            "发货地在东京都内。【只有拉过详情的商品才知道发货地】"
                            "三个源都只在详情里给这个字段，搜索结果里没有——"
                            "没这个标不等于不在东京，可能只是还没拉详情")
                    if fresh == "listed":
                        ui.badge("新上架", color="orange").tooltip(
                            f"平台显示它是最近 {fresh_hours} 小时内挂出来的")
                    elif fresh == "found":
                        ui.badge("新发现", color="blue-grey").tooltip(
                            f"最近 {fresh_hours} 小时内才进我们的库。商品本身可能"
                            "早就挂着了 —— 多半是它降价进了你的价格区间。"
                            "ヤフオク 不提供上架时间，它的商品只会有这个标")
                    if r["desc_warn"] == poller.DESC_UNREAD:
                        # 【这不是警示词，是降级信号】详情页打开了但描述没解析出来
                        # （多半是平台改版）。画成普通警示徽标、还配一句
                        # 「可能是卖家在否认」的解释，等于用假信息盖住了
                        # 「警示层对这件商品整个失效」这个事实。
                        ui.badge("描述未读到", color="grey").tooltip(
                            "详情页打开了，但描述没解析出来（平台页面结构可能变了）。"
                            "这件商品的描述警示【没有生效】，点进去自己看一眼")
                    elif r["desc_warn"]:
                        # 描述里命中了警示词。商品没被毙掉，只是提醒你点开看一眼
                        ui.badge(f"描述: {r['desc_warn']}", color="amber") \
                            .tooltip("描述里出现了这些词，但可能是卖家在否认（如"
                                     "「ジャンク品ではありません」）。点标题自己看一眼")
            # 标题【不截断】，长了就换行（break-words 让超长的连续
            # 字符串——比如日文长串型号——也能断开，不会撑破容器）
            ui.link(r["name"], item_url(r["source"], r["item_id"]),
                    new_tab=True).classes("font-medium break-words")
            note, color = auction_note(r)
            if note:
                ui.label(note).classes(f"text-xs {color}")
            # 【mt-auto 把这一行顶到卡片底部】宽屏两列时，同一行的两张
            # 卡片高度取决于高的那张；不顶到底部的话，矮的那张这一行会
            # 悬在半空，两列的这排小字对不齐，扫起来很累。
            # 配合正文列的 self-stretch（让它撑满卡片高度）才生效。
            with ui.row().classes(
                    "gap-3 text-xs text-gray-400 items-center mt-auto pt-1"):
                # 【固定顺序】来源 → 品相 → 发货地 → 上架时间。
                # 来源排第一，是因为它【每一行都有】—— 打头的那一项必须恒定存在，
                # 否则两列卡片的这一行会从不同的位置起头，横着扫过去对不齐。
                # 后面三项可有可无（ヤフオク 不给上架时间、没拉详情就没有发货地），
                # 它们缺了只是这一行短一截，不影响起点。
                # 【别再往这里塞会变的信息】它贴在卡片底部、两列之间要横向对齐。
                # 会变的一律做成徽标放标题上方（降价就是这么挪上去的）。
                ui.badge(source_name(r["source"])).classes(BADGE_LABEL)
                ui.label(COND.get(r["condition_id"], "品相未标"))
                # 非东京的也显示出来 —— 不然「这件为什么没有东京标」
                # 你分不清是「不在东京」还是「还没拉详情」
                if r["ship_from"]:
                    ui.label(f"发货 {r['ship_from']}")
                if r["listed_at"]:
                    # 【没有上架时间就整个不渲染】ヤフオク 不给这个字段，
                    # 渲染成空 label 会在两列之间留一个对不齐的空档
                    ui.label(f"上架 {r['listed_at']:%m-%d %H:%M}")

        # shrink-0：价格列宽度固定，不参与压缩
        # whitespace-nowrap：¥1,188,800 这种数字本身也绝不折行
        # self-stretch：和正文列一样撑满卡片高度，下面拉黑按钮的 mt-auto
        # 才能把它压到底 —— 否则它悬在半空，和左边那行小字不在同一条基线上。
        with ui.column().classes(
                "gap-0 items-end shrink-0 whitespace-nowrap self-stretch"):
            # 来源放在价格正上方：这两个信息是一起看的 ——
            # 同一个价格在哪个平台，直接决定你怎么去买
            ui.label(yen(r["price"])).classes("text-lg font-bold")
            if r["deal_pct"]:
                # 【颜色按百分比本身，不按 is_deal】跟着 is_deal 走的话，
                # 手动捡漏价一开，「市价的 110%」会显示成绿色 —— 绿色读作
                # 「便宜」，而 110% 是贵了一成，界面在自相矛盾。
                # 是不是捡漏由上面那个绿徽标说，这里只说贵还是便宜。
                ui.label(f"市价的 {r['deal_pct']}%").classes(
                    "text-xs " + ("text-green-400" if r["deal_pct"] < 100
                                  else "text-gray-400"))
            price_history(r, prev)
            # 追踪已经挪到图片角上的星了，这里剩「剔除这一件」和「拉黑这个人」。
            # 【两个挨着放是有意的】它们长得像、作用范围差一个数量级：
            # 剔除只管眼前这一条，拉黑是【所有规则、所有源】下这个卖家的全部商品。
            # 并排摆着、一个灰一个红，按之前先看清自己在按哪个。
            with ui.row().classes("items-center gap-1 mt-auto"):
                # 【必须用默认参数绑死 row/rn】r 和 rule 是循环变量，直接引用的话
                # 等你点下去时早就指向最后一件商品了 —— 每个按钮剔除同一件。
                ui.button(
                    "剔除",
                    on_click=lambda _, row=dict(r), rn=rule["name"]:
                        toggle_hide(row, rn, True),
                ).props(BTN_QUIET).classes("btn-muted row-act") \
                 .tooltip("让它不在命中页显示、也不再推送到手机。商品照常抓取、"
                          "照常对账，全部页和追踪页都还看得到。"
                          "剔除的是这个链接本身，它在别的规则下也不再显示。"
                          "要拿回来：命中页最下面那块「已剔除」")
                # 【必须用默认参数绑死 sid/row】它们是循环变量，直接引用的话
                # 等你点下去时早就指向最后一件商品了 —— 每个按钮拉黑同一个人。
                # 卖家ID为空时不给按钮：ヤフオク 有一部分商品不给卖家ID，
                # 没有可拉黑的对象，画个点不动的按钮只会让人以为坏了。
                # 【这里不需要「已拉黑」态】被拉黑的商品 matched=0，根本进不了
                # live_matched，这一页上永远看不到已拉黑的人（「全部」页才需要）。
                if r["seller_id"]:
                    ui.button(
                        "拉黑卖家",
                        on_click=lambda _, sid=r["seller_id"], row=dict(r):
                            blacklist_seller(sid, row),
                    ).props(BTN_DANGER).classes("btn-muted row-act") \
                     .tooltip(
                        f"卖家 {r['seller_id']}\n"
                        "拉黑后【所有规则】下他的商品立刻判为不合适，也不再推送，"
                        "商品本身照常抓取。要解除：「规则」页最下面的「拉黑列表」")


def _can_blacklist(row: dict, blocked: frozenset | set) -> bool:
    """这一行能不能拉黑：有卖家ID、且还没在全局拉黑列表里。

    【列表必须由调用方传进来，不许在这里现查】它在「全部」页的行数据推导里
    每行调一次，300 行就是 300 次往返。store.blocked_sellers() 虽然走缓存，
    但那份缓存 10 秒会过期一次，正好卡在渲染中间就是一次真查询。
    """
    sid = (row.get("seller_id") or "").strip().lower()
    return bool(sid) and sid not in blocked


def _act_label(row: dict, blocked: frozenset | set) -> str:
    if not (row.get("seller_id") or "").strip():
        return "—"
    return "拉黑" if _can_blacklist(row, blocked) else "已拉黑"


def save_deal_price(rule_id: int, value) -> None:
    """设手动捡漏价，并【立刻】重算一遍，不用等下一轮。

    立刻重算是因为这是个手动动作：你就是想马上看到哪些变成捡漏了。
    mark_deals 是纯 CPU、不发请求，同步跑一次只是几十行 UPDATE。
    """
    try:
        v = int(float(value or 0))
    except (TypeError, ValueError):
        notify("捡漏价要填数字", type="warning")
        return
    if v < 0:
        notify("捡漏价不能是负数", type="warning")
        return
    rule = store.get_rule(rule_id)
    if not rule:
        notify("这条规则不在了（可能刚被删掉）", type="warning")
        return
    store.update_rule(rule_id, {"deal_price": v})
    poller.mark_deals(store.get_rule(rule_id))
    if v:
        notify(f"「{rule['name']}」捡漏价设为 {yen(v)}，已重算")
    else:
        notify(f"「{rule['name']}」已清空手动价，回到按成交中位数的百分比判")
    hits_view.refresh()
    sold_view.refresh()


# ------------------------------------------------------------------ 追踪页

@ui.refreshable
def track_view() -> None:
    """追踪中的商品。这一页的数据比别处都新 —— 它们是被单独拉详情刷新的。"""
    rows = store.tracked_items()
    marks = store.marked_ids()
    twins: dict = {}
    # 【这一页最需要「上次价格」】追踪的意义就是盯它动没动。只给一个当前价，
    # 你得记住上次打开时是多少 —— 而这正是你把它加进追踪的原因。
    prevs = store.prev_prices(rows)
    st = store.get_settings()
    if not rows:
        ui.label("还没有追踪任何商品。在「命中」页每件商品右下角点「追踪」加进来。"
                 ).classes("text-gray-400 text-sm")
        return

    rules = {r["id"]: r for r in store.get_rules()}
    # 【分母只能是还在刷新的那些】卖掉/下架的会留在这一页（你盯的东西的结局
    # 属于这里），但它们一个请求都不再发。拿总件数去估每天烧多少，
    # 挂的死货越多这个数越假 —— 而这个数正是用来决定"还能不能再追一件"的。
    live = sum(1 for r in rows if r["status"] in store.LIVE)
    per_day = int(live * (1440 / max(st["track_min"], 1)))
    done = len(rows) - live
    ui.label(f"在追 {live} 件 · 每 {st['track_min']} 分钟刷新一次（交易中的每 "
             f"{st.get('trading_recheck_min') or st['track_min']} 分钟）、每轮最多 "
             f"{st['track_budget']} 件 · 满负荷约 {per_day:,} 次请求/天"
             f"（今日上限 {st['daily_request_limit']:,}）"
             + (f" · 另有 {done} 件已结束，留着看结果，不再发请求" if done else "")
             ).classes("text-xs text-gray-400 mb-3")

    with ui.element("div").classes("grid grid-cols-1 xl:grid-cols-2 gap-x-6 w-full"):
        for r in rows:
            _track_row(r, rules.get(r["rule_id"]) or {}, st, marks, prevs, twins)


def _track_row(r: dict, rule: dict, st: dict, marks: set, prevs: dict,
               twins: dict) -> None:
    """布局和命中页同一套，理由见那边的注释。"""
    with ui.row().classes("items-start w-full gap-3 border-t py-2 sm:flex-nowrap"):
        # 和命中页同一颗星：这里它一定是实心的，点一下就是取消追踪
        thumb_corners(r, r["rule_id"], marks, rule.get("name", ""), twins=twins)
        with ui.column().classes("gap-0 flex-1 min-w-0 leading-snug self-stretch"):
            with ui.row().classes("items-center gap-2 flex-wrap mb-1"):
                # 【终态的留在这一页】卖掉/下架的不会被摘掉追踪，所以这三种
                # 状态都会出现。已卖掉排在最前面判，因为它一旦成立，
                # 「捡漏」「已降」这些还能不能买的信息就都没意义了。
                if r["status"] == "sold_out":
                    ui.badge("已卖掉", color="grey").tooltip("最终成交价见右边")
                elif r["status"] == "gone":
                    ui.badge("已下架", color="grey").tooltip(
                        "商品页没了。可能是卖家撤了，也可能是平台删了")
                elif r["status"] == "trading":
                    ui.badge("交易中", color="amber").tooltip(
                        "有人已经在买了。Mercari 上付款后会先进这个状态，成交才变已售出")
                elif r["is_deal"]:
                    ui.badge("捡漏", color="green")
                if r["price"] < r["first_price"]:
                    ui.badge(f"已降 {yen(r['first_price'] - r['price'])}", color="red")
                if r["ship_from"] == TOKYO:
                    ui.badge(TOKYO, color="purple")
                ui.label(rule.get("name", "?")).classes("text-xs text-gray-400")
            ui.link(r["name"], item_url(r["source"], r["item_id"]),
                    new_tab=True).classes("font-medium break-words")
            note, color = auction_note(r)
            if note:
                ui.label(note).classes(f"text-xs {color}")
            with ui.row().classes(
                    "gap-3 text-xs text-gray-400 items-center mt-auto pt-1"):
                # 【上次刷新要显示出来】这一页的卖点就是"数据比别处新"，
                # 不把刷新时间摆出来，人没法判断眼前这个价还作不作数。
                # 但已成交的不一样：它的价格不会再变了，"刷新时间"毫无意义，
                # 该说的是它什么时候成交的。
                if r["status"] == "sold_out" and r["sold_at"]:
                    ui.label(f"{r['sold_at']:%m-%d %H:%M} 成交")
                else:
                    ui.label(f"{r['last_seen_at']:%m-%d %H:%M} 刷新")
                if r["ship_from"]:
                    ui.label(f"发货 {r['ship_from']}")
        with ui.column().classes(
                "gap-0 items-end shrink-0 whitespace-nowrap self-stretch"):
            ui.label(yen(r["price"])).classes("text-lg font-bold")
            if r["deal_pct"]:
                ui.label(f"市价的 {r['deal_pct']}%").classes(
                    "text-xs " + ("text-green-400" if r["deal_pct"] < 100 else "text-gray-400"))
            price_history(r, prevs.get((r["source"], r["item_id"])))
            # 取消追踪已经在图片角的星上，这里不再重复一个按钮 ——
            # 同一个动作出现两次，人会以为它们不是一回事。


# ------------------------------------------------------------------ 标记页

@ui.refreshable
def marks_view() -> None:
    """标记过的商品。纯书签：不发请求、不参与任何判定、不影响任何规则。

    【这一页同时显示两份数据，缺一个都不成立】
      快照 —— 按下标记那一刻的标题、价格、图。存在 marked_item 表里，之后不再变。
      现况 —— 那件商品现在怎么样了（现查 item 表）。商品被删、规则被删之后就没有了。
    只有快照，这一页就是一堆旧照片，看不出"后来降了没、卖了没"；
    只有现况，商品一下架整条记录就变成空白，而那正是你最想回头查的时候。
    """
    rows = store.marked_items()
    if not rows:
        ui.label("还没有标记任何商品。在「命中」「追踪」「成交」页，"
                 "点商品图【右下角】那面小旗 ⚐ 就标上了 —— 不发任何请求，随便标。"
                 ).classes("text-gray-400 text-sm")
        return
    ui.label(f"共 {len(rows)} 件 · 按标记时间倒序 · 纯记录：不发请求、不参与捡漏判定"
             ).classes("text-xs text-gray-400 mb-3")
    with ui.element("div").classes("grid grid-cols-1 xl:grid-cols-2 gap-x-6 w-full"):
        for m in rows:
            _mark_row(m)


def _mark_row(m: dict) -> None:
    """布局和命中页同一套，理由见那边的注释。"""
    live = m["live"]
    with ui.row().classes("items-start w-full gap-3 border-t py-2 sm:flex-nowrap"):
        # 这一页上的旗一定是实心的，点一下就是取消标记。
        # rule_id 传 0：star=False 时它用不到（追踪才需要规则维度）。
        thumb_corners(m, 0, {(m["source"], m["item_id"])}, m["rule_name"], star=False)
        with ui.column().classes("gap-0 flex-1 min-w-0 leading-snug self-stretch"):
            with ui.row().classes("items-center gap-2 flex-wrap mb-1"):
                if live is None:
                    ui.badge("已不在库", color="grey").tooltip(
                        "抓取记录没了（多半是那条规则被删了，或者商品早就下架不再被抓到）。"
                        "标记本身不受影响 —— 下面显示的就是你标记那一刻的快照")
                elif live["status"] == "sold_out":
                    ui.badge("已卖掉", color="grey")
                elif live["status"] == "gone":
                    ui.badge("已下架", color="grey")
                if m["rule_name"]:
                    ui.label(m["rule_name"]).classes("text-xs text-gray-400")
            ui.link(m["name"], item_url(m["source"], m["item_id"]),
                    new_tab=True).classes("font-medium break-words")
            # 【备注是这一页的重点】"以后好查"靠的就是它：过两个月回来看，
            # 光有标题和价格想不起来当初为什么标它。
            # 保存键放进 append 插槽，理由同命中页的捡漏价输入框。
            inp = ui.input(value=m["note"], placeholder="记一句：为什么标它") \
                .props(INPUT).classes("w-full max-w-md mt-1")
            with inp.add_slot("append"):
                # 【必须用默认参数绑死】m 和 inp 都是循环变量
                ui.button("保存", on_click=lambda _, so=m["source"], ii=m["item_id"],
                          c=inp: save_mark_note(so, ii, c.value)) \
                    .props(BTN_GHOST).classes("px-2")
            with ui.row().classes(
                    "gap-3 text-xs text-gray-400 items-center mt-auto pt-1"):
                ui.label(f"{m['marked_at']:%Y-%m-%d %H:%M} 标记")
        with ui.column().classes(
                "gap-0 items-end shrink-0 whitespace-nowrap self-stretch"):
            ui.label(yen(m["price"])).classes("text-lg font-bold")
            ui.label("标记时").classes("text-xs text-gray-500")
            # 现价只在【和当初不一样】时才显示 —— 一样的时候多写一行纯噪音
            if live and live["price"] != m["price"]:
                diff = live["price"] - m["price"]
                ui.label(f"现 {yen(live['price'])}").classes(
                    "text-sm mt-1 " + ("text-green-400" if diff < 0 else "text-gray-400"))
                ui.label(("↓ " if diff < 0 else "↑ ") + yen(abs(diff))).classes(
                    "text-xs " + ("text-green-400" if diff < 0 else "text-gray-500"))


# ------------------------------------------------------------------ 成交页

def _load_sold() -> dict:
    """成交页要的数据，5 条 SQL 取完（原先 14 条：每条规则 3 条）。"""
    return {"rules": store.get_rules(), "marks": store.marked_ids(),
            "states": store.rule_states(), "tracked": store.sold_tracked_all(),
            "samples": store.sold_samples_recent(80)}


# 【@ui.refreshable 必须挂在画界面的那个函数上，不能挂在取数的 _load_* 上】
# 挂错的后果不是"刷新没反应"，是【整页打不开】：装饰器一走，sold_view 就是个
# 普通函数，没有 .refresh 属性，而 build_sold 里那个「刷新」按钮在【建页签的时候】
# 就要读 sold_view.refresh —— 于是切到成交页当场抛 AttributeError，页签建到一半
# 中断，页面上一片空白，而错误只在 journal 里。实际发生过（2026-09-17）。
# 取数函数不画任何东西，对它 refresh() 只会重跑一遍 SQL 再把结果丢掉，永远不重画。
# 对照 _load_hits / hits_view：那一对是对的，照着写。
@ui.refreshable
def sold_view() -> None:
    """成交页：市场实际用什么价把什么货清掉了。

    【为什么要单独一栏】命中页回答「现在有什么能买」，靠的是和市价中位数比。
    但那个中位数是个单一数字，看不出它底下的分布：是十几件挤在一个价位，
    还是从 40 万到 90 万拉了一条长线。定价前要看的是后者。

    两类数据粒度不同，都得摆出来：
      跟到成交的  item.status='sold_out'，有标题缩略图，能看清「什么货、什么价、
                  降了多少才卖掉」。只有走完「我们一直在跟 → 它从搜索结果消失 →
                  对账确认售出」这条链路的才会进来，所以量少但信息最全。
      成交价样本  sold_sample，只有价格和时间（成交轮按标题级规则扫来的，没拉详情），
                  量大，是中位数的实际依据。
    """
    d = _load_sold()
    rules = d["rules"]
    if not rules:
        ui.label("还没有规则，去「规则」页新建一条。").classes("text-gray-400 p-4")
        return

    marks = d["marks"]
    for rule in rules:
        rid = rule["id"]
        st = d["states"].get(rid) or store.STATE_DEFAULT
        med = st["median_price"]
        # 【必须加 matched = 1】不加的话这一页会混进被规则判掉的货：
        # 部品取り、GPUなし、纯散热器、笔记本 —— 实测 19 件里有 13 件是这类，
        # 价格从 ¥1,600 到 ¥14,000。它们和你要的卡不是一个东西，
        # 摆在成交页上只会把"这个货色卖多少钱"这个判断整个带偏。
        # （matched = 1 的条件在 store.sold_tracked_all 的 SQL 里）
        tracked = d["tracked"].get(rid, [])
        samples = d["samples"].get(rid, [])

        # 折叠摘要：折起来后这一行就是全部，所以中位数和价格区间都要在里面
        cap = [f"近{rule['median_window_days']}天"]
        if samples:
            ps = sorted(x["price"] for x in samples)
            cap.append(f"成交 {len(samples)} 件　{yen(ps[0])} 〜 {yen(ps[-1])}")
        if med:
            cap.append(f"中位 {yen(med)}")
        else:
            cap.append(f"样本不足 {rule['median_min_samples']} 件，暂不出中位数")
        # 【捡漏线按当前生效的那条写】手动价一填就盖过百分比（core/matcher.is_deal），
        # 原先这里只按百分比算 —— 四条规则都填了手动价，显示的全是不生效的那条线。
        line = deal_line(rule, med)
        if line:
            cap.append(line)
        head = f"{rule['name']}　成交样本 {len(samples)} 件" + (
            f"　🔗 跟到成交 {len(tracked)} 件" if tracked else "")

        with ui.expansion(head, caption="　·　".join(cap), value=True) \
                .classes(CARD).props("header-class=text-base"):
            if not samples and not tracked:
                ui.label("还没有成交数据。成交轮每 "
                         f"{rule['sold_scan_hours']} 小时跑一次，跑过之后这里才有东西。"
                         ).classes("text-gray-400 text-sm")
                continue

            if not tracked:
                # 【没有「跟到成交」的时候也要说一句话】成交样本只汇总进上面那行摘要
                # （它们只有价格和时间，逐条列出来没有信息量），所以这一块会整个空掉 ——
                # 展开一个空盒子，看起来和"页面坏了"一模一样，而它其实是正常状态。
                ui.label(f"这 {len(samples)} 件成交样本只有价格和时间（成交轮按标题级规则"
                         "扫来的，没拉详情），所以只汇总在上面那一行。"
                         "下面这一块列的是「跟到成交」的商品 —— 要走完"
                         "「一直在跟 → 从搜索结果消失 → 对账确认售出」才算，攒得慢。"
                         ).classes("text-sm text-gray-400 mt-1")

            if tracked:
                ui.label("我们一路跟到成交的（拉过详情、过了完整规则，信息最全）") \
                    .classes("text-sm text-gray-400 mt-1")
                with ui.element("div").classes(
                        "grid grid-cols-1 xl:grid-cols-2 gap-x-6 w-full"):
                    for r in tracked:
                        _sold_row(r, med, marks, rule["name"])


def _sold_row(r: dict, med: int | None, marks: set, rule_name: str) -> None:
    """一件跟到成交的商品。布局和命中页同一套，理由见那边的注释。"""
    with ui.row().classes("items-start w-full gap-3 border-t py-2 sm:flex-nowrap"):
        # star=False：已经卖掉的东西追踪它没意义（不会再刷新），
        # 但把"这个货色什么价成交的"记一笔很有意义 —— 所以只留标记旗。
        thumb_corners(r, r["rule_id"], marks, rule_name, star=False)
        # self-stretch + 下面那行的 mt-auto：和命中页同一套贴底做法。
        # 漏了的话宽屏两列时矮的那张卡片小字悬在半空，两列对不齐。
        with ui.column().classes("gap-0 flex-1 min-w-0 leading-snug self-stretch"):
            with ui.row().classes("items-center gap-2 flex-wrap mb-1"):
                # 【这里不放「已成交」徽标】这一整页就是成交记录，每行再标一次
                # 等于没说，而它还占着每行第一个徽标位 —— 真正有信息的
                # 「東京都」「曾是捡漏」被它挤到后面去了。
                if r["ship_from"] == TOKYO:
                    ui.badge(TOKYO, color="purple")
                if r["is_deal"]:
                    # 卖掉的捡漏货 = 你错过的那些。摆出来是为了让你知道
                    # 这个价位真的会被人买走，下次别犹豫。
                    ui.badge("曾是捡漏", color="green").tooltip(
                        "它在售时低于捡漏线 —— 也就是这个价位确实有人接")
            ui.link(r["name"], item_url(r["source"], r["item_id"]),
                    new_tab=True).classes("font-medium break-words")
            with ui.row().classes(
                    "gap-3 text-xs text-gray-400 items-center mt-auto pt-1"):
                ui.label(f"{r['sold_at']:%m-%d %H:%M} 成交" if r["sold_at"] else "成交时间未知")
                if r["ship_from"]:
                    ui.label(f"发货 {r['ship_from']}")
                # 【降了多少才卖掉】这是定价最直接的参考：挂多少没人要、降到多少成交
                if r["price"] < r["first_price"]:
                    ui.label(f"从 {yen(r['first_price'])} 降了 "
                             f"{yen(r['first_price'] - r['price'])} 才卖掉").classes("text-red-400")
        with ui.column().classes("gap-0 items-end shrink-0 whitespace-nowrap"):
            ui.label(yen(r["price"])).classes("text-lg font-bold")
            if med:
                ui.label(f"市价的 {r['price'] * 100 // med}%").classes("text-xs text-gray-400")




# ------------------------------------------------------------------ 全部页

@ui.refreshable
def all_view(rule_id: int | None, reason: str, q: str = "") -> None:
    where, args = ["1=1"], []
    if rule_id:
        where.append("rule_id = %s"); args.append(rule_id)
    if reason != "全部":
        key = next(k for k, v in REASON_LABEL.items() if v == reason)
        where.append("reject_reason = %s"); args.append(key)
    q = (q or "").strip()
    if q:
        # 【搜标题和卖家 ID】300 行按规则/原因翻找一件具体的东西太慢；
        # 卖家 ID 也能搜 —— 拉黑之前想看看这个人还挂了什么。
        where.append("(name LIKE %s OR seller_id LIKE %s)")
        args += [f"%{q}%", f"%{q}%"]

    rows = store.query(
        # 【必须覆盖 explain() 读的每一个字段】少一个，「具体原因」那一列就会
        # 静默退化成兜底文案（seller_id 漏掉时每行都显示「卖家 ? 在拉黑列表里」），
        # 而这一列存在的全部意义就是说清楚是哪个条件、哪个值判掉的。
        f"SELECT source, item_id, rule_id, name, price, matched, reject_reason, status, "
        f"condition_id, seller_id, desc_warn, desc_checked, bid_count, buy_now_price, "
        f"end_time, first_seen_at FROM item "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY first_seen_at DESC LIMIT 300", args)
    # 拿完整规则（不只是名字）：原因列要用规则里的词表和阈值把「为什么」算出来
    rules = {r["id"]: r for r in store.get_rules()}
    # blocked_sellers() 和上面 get_rules 里的 _with_settings 走的是同一个
    # get_settings() 缓存入口，所以这一行放前放后都不多发查询（实测都是 3 条）。
    blocked = store.blocked_sellers()

    ui.label(f"{len(rows)} 件（最多显示 300 件，按发现时间倒序）").classes("text-sm text-gray-400")
    tbl = ui.table(
        columns=[
            {"name": "rule", "label": "规则", "field": "rule", "align": "left"},
            {"name": "src", "label": "来源", "field": "src", "align": "left", "sortable": True},
            # 【放原始数字，不放格式化字符串】Quasar 的默认排序先判 isNumber，
            # 拿到 "¥1,188,800" 这种字符串就退化成字典序 —— 升序会排成
            # ¥1,188,800 < ¥12,000 < ¥2,000，最贵的跑到最前面。
            # 而这一页正是用来「按价格找是不是上限卡太死」的，顺序假了就白看。
            # 显示交给下面的 body-cell-price 插槽。
            {"name": "price", "label": "价格", "field": "price", "align": "right", "sortable": True},
            {"name": "reason", "label": "判定", "field": "reason", "align": "left", "sortable": True},
            {"name": "detail", "label": "具体原因", "field": "detail", "align": "left"},
            {"name": "status", "label": "状态", "field": "status", "align": "left"},
            # 【卖家列必须排在标题前面】标题是贪婪列（没给宽度，吃掉剩余空间），
            # 把卖家放它后面的话，整列会被挤出视口 —— 那一页的拉黑按钮就点不到了，
            # 而且表格横向滚动条在暗色下几乎看不见，人根本不知道右边还有东西。
            {"name": "act", "label": "卖家", "field": "act", "align": "left"},
            {"name": "name", "label": "标题（点击打开商品页）", "field": "name", "align": "left"},
        ],
        rows=[{
            "id": f"{r['source']}-{r['item_id']}-{r['rule_id']}",
            "rule": (rules.get(r["rule_id"]) or {}).get("name", "?"),
            "src": source_name(r["source"]),
            "price": r["price"],
            "reason": REASON_LABEL.get(r["reject_reason"], r["reject_reason"]),
            "detail": explain(rules.get(r["rule_id"]) or {}, r),
            "status": {"on_sale": "在售", "sold_out": "已售出",
                       "trading": "交易中", "gone": "已下架"}.get(r["status"], r["status"]),
            "name": r["name"],
            # 下面四个不作为列显示，只放在行数据里给模板和事件回调取用。
            # src_key / iid / name 是拉黑时要存的那份商品快照（列表里拼成可点的链接）。
            "link": item_url(r["source"], r["item_id"]),
            "seller": r["seller_id"],
            "src_key": r["source"],
            "iid": r["item_id"],
            # 【按钮的文案和可点性在这里算好】插槽模板每行只实例化一份同样的元素，
            # Python 侧没法逐行控制，所以条件得预先算成行数据、模板里只做绑定。
            "act": _act_label(r, blocked),
            "can_bl": _can_blacklist(r, blocked),
        } for r in rows],
        row_key="id", pagination=50,
    )
    # 【两个 add_slot 必须分开写】Element.add_slot() 返回的是 Slot 不是 Element，
    # 链式接第二个 add_slot 会 AttributeError: 'Slot' object has no attribute 'add_slot'
    tbl.add_slot("body-cell-price", r'''
        <q-td :props="props" class="text-right">
          {{ props.value == null ? "—" : "¥" + Number(props.value).toLocaleString() }}
        </q-td>
    ''')
    # 【必须放开 white-space 并封宽】Quasar 的表格单元格默认 nowrap，标题又是贪婪列，
    # 于是长标题直接被切在视口边缘 —— 没有省略号，看不出后面还有字。
    # 这一页正是用来核对「是哪个词把它判掉的」，而被切掉的往往就是那个词。
    # 所以让它折行而不是省略：宁可行高不齐，也不能把判定依据藏起来。
    tbl.add_slot("body-cell-name", r'''
        <q-td :props="props" style="white-space:normal;max-width:34rem">
          <a :href="props.row.link" target="_blank" rel="noopener noreferrer"
             class="text-blue-400 hover:underline break-words">{{ props.value }}</a>
        </q-td>
    ''')
    # 【这一格用 NiceGUI 元素，不用裸 HTML】上面两个插槽是纯展示，裸模板就够了；
    # 这一格要回调到 Python，走 table.cell + 元素自己的 on() 是官方支持的路径，
    # 不用去赌 `$parent.$emit` 在 scoped slot 里指向哪个组件。
    # 文案三态：没有卖家ID → 「—」（ヤフオク 有一部分商品不给，メルカリShops
    # 的卖家是店铺不是用户）；已经在拉黑列表里 → 「已拉黑」；其余才可点。
    with tbl.add_slot("body-cell-act"):
        with tbl.cell("act"):
            ui.button().props(
                BTN_DANGER + ' '
                ':label="props.row.act" :disable="!props.row.can_bl"'
            ).on(
                "click",
                # 【必须多参数 emit，不要包成一个数组】nicegui.js 的
                # stringifyEventArgs 永远返回数组，所以 e.args 本来就是 list：
                # emit(x) 时 e.args == [x]，而 emit([x]) 时 e.args == [[x]] ——
                # e.args[0] 拿到的是【列表】，blacklist_seller 里一句 .strip() 就
                # AttributeError，按钮点下去什么都不发生，异常被 NiceGUI 吞进日志。
                js_handler="() => emit(props.row.seller, props.row.src_key, "
                           "props.row.iid, props.row.name)",
                handler=lambda e: blacklist_seller(
                    e.args[0], {"source": e.args[1], "item_id": e.args[2],
                                "name": e.args[3]}),
            )


# ------------------------------------------------------------------ 规则页

def rule_dialog(rule: dict | None, host=None) -> None:
    """新建/编辑规则。rule=None 表示新建。

    【host 必须是页面级容器，不能省】对话框默认会建在「触发它的那个按钮」所在的
    插槽里，也就是 rules_view 的刷新容器内部。而 refreshable.refresh() 的第一步是
    container.clear()，会把所有后代删掉 —— 于是：你点「立即跑一轮」（要跑几分钟），
    等待期间去编辑另一条规则的排除词表，那一轮跑完时 run_now 末尾无条件
    rules_view.refresh()，对话框连同你刚敲的一大段内容凭空消失，界面上只弹一个
    绿色的「完成」，没有任何提示说你的编辑被吃了。
    """
    data = dict(rule) if rule else {
        "name": "", "enabled": 1, "keyword": "", "sources": "",
        "include_all": "", "include_any": "",
        "exclude_any": "", "warn_desc": "",
        "price_min": 0, "price_max": 0,
        "condition_ids": "", "allow_shops": 0, "check_desc": 1,
        "deal_price": 0, "deal_ratio": 85, "quick_min": 7, "note": "",
    }
    with (host or ui.context.client.content):
        dlg = ui.dialog()
    with dlg, ui.card().classes("w-[760px] max-w-full"):
        ui.label("编辑规则" if rule else "新建规则").classes("text-lg font-bold")
        with ui.column().classes("w-full gap-2"):
            for f in ("name", "keyword", "include_all", "include_any"):
                ui.input(f, value=data[f]).classes("w-full").props(INPUT) \
                    .bind_value(data, f).tooltip(FIELD_HELP.get(f, ""))
                ui.label(FIELD_HELP.get(f, "")).classes("text-xs text-gray-400 -mt-2")
            # 【卖家黑名单不在这里了】2026-09-17 改成全局的拉黑列表，在规则页最下面。
            for f in ("exclude_any", "warn_desc"):
                ui.textarea(f, value=data[f]).classes("w-full").props(INPUT + " rows=3") \
                    .bind_value(data, f)
                ui.label(FIELD_HELP.get(f, "")).classes("text-xs text-gray-400 -mt-2")
            with ui.row().classes("w-full gap-3"):
                ui.number("价格下限 ¥", value=data["price_min"], format="%d") \
                    .props(INPUT).bind_value(data, "price_min")
                ui.number("价格上限 ¥（0=不限）", value=data["price_max"], format="%d") \
                    .props(INPUT).bind_value(data, "price_max")
                ui.number("手动捡漏价 ¥（0=不用）", value=data["deal_price"], format="%d") \
                    .props(INPUT).bind_value(data, "deal_price") \
                    .tooltip("低于它就算捡漏。【填了就完全盖过右边的百分比】"
                             "它的意义是不依赖成交样本 —— 规则刚建、或某个型号成交太少"
                             "算不出中位数时，百分比那套整个不工作，而你心里是有价的")
                ui.number("捡漏线 %", value=data["deal_ratio"], format="%d") \
                    .props(INPUT).bind_value(data, "deal_ratio") \
                    .tooltip("低于「成交中位数 × 此值%」时标捡漏。0=关闭。"
                             "左边填了手动价的话这一项不生效")
                ui.number("扫描间隔 分", value=data["quick_min"], format="%d", min=1) \
                    .props(INPUT).bind_value(data, "quick_min") \
                    .tooltip("至少 1 分钟。这个值直接决定对外发请求的频率")
            ui.input("品相白名单", value=data["condition_ids"]).classes("w-full") \
                .props(INPUT).bind_value(data, "condition_ids") \
                .tooltip(FIELD_HELP["condition_ids"])
            ui.label(FIELD_HELP["condition_ids"]).classes("text-xs text-gray-400 -mt-2")
            ui.input("备注", value=data["note"]).classes("w-full").props(INPUT) \
                .bind_value(data, "note")
            # 数据源多选：全不选＝全部源（和 sources 列留空等价）
            all_keys = list(sources.all_sources())
            picked = {k: (k in (data["sources"] or "").split(",")) for k in all_keys}
            with ui.row().classes("items-center gap-4"):
                ui.label("数据源").classes("text-sm")
                for k in all_keys:
                    ui.switch(sources.get(k).name, value=picked[k]).bind_value(picked, k)
            ui.label("全不选 = 全部源。各源的限速状态（退避档位、上次请求时间）互相独立，"
                     "但轮询只有一条线程 —— 某个源退避期间，别的源那一轮会被顺延"
                     ).classes("text-xs text-gray-400 -mt-2")

            with ui.row().classes("gap-6"):
                ui.switch("启用", value=bool(data["enabled"])).bind_value(data, "enabled")
                ui.switch("收 Shops 商家品", value=bool(data["allow_shops"])) \
                    .bind_value(data, "allow_shops")
                ui.switch("拉详情查描述", value=bool(data["check_desc"])) \
                    .bind_value(data, "check_desc")

        def save() -> None:
            chosen = [k for k, v in picked.items() if v]
            data["sources"] = "" if len(chosen) == len(all_keys) else ",".join(chosen)
            if not data["name"] or not data["keyword"]:
                notify("规则名和搜索词不能为空", type="warning")
                return
            # 【quick_min 必须 ≥1，不能走 clean() 的 None→0】
            # ui.number 被清空时 value 是 None，粘贴「7 分钟」这种带非数字的文本也是 None。
            # 一旦落成 0，poller 的 _due(last, 0) 恒为真 —— 这条规则的每个源都会在
            # 每 30 秒一次的 tick 上被整轮重扫，几小时就撞满 daily_request_limit，
            # 然后【所有规则所有源】一起停抓到次日。
            # 其它数字框的 0 是合法语义（价格上下限 0=不限、捡漏线 0=关闭），所以只拦这一个。
            try:
                qm = int(data.get("quick_min") or 0)
            except (TypeError, ValueError):
                qm = 0
            if qm < 1:
                notify("扫描间隔至少 1 分钟（填 0 会让这条规则每 30 秒全源重扫，"
                       "几小时就会烧穿当天的请求配额）", type="warning")
                return
            data["quick_min"] = qm
            # 这三个清空＝按各自的「不限 / 关闭」语义走，是合法的
            for k in ("price_min", "price_max", "deal_ratio", "deal_price"):
                if data.get(k) is None:
                    data[k] = 0
            # ui.number 被清空时 value 是 None，而这些列都是 NOT NULL；
            # ui.switch 给的是 bool，ui.number 给的是 float —— 统一收成 int。
            def clean(v):
                if isinstance(v, bool):
                    return int(v)
                if isinstance(v, float) or v is None:
                    return int(v or 0)
                return v

            payload = {k: clean(v) for k, v in data.items() if k in store.RULE_FIELDS}
            if rule:
                # 【只写真正改过的字段，不要整行覆盖】data 是打开对话框那一刻的快照，
                # 而这个对话框是刻意建在 dialog_host 里的（要能撑过几分钟的「立即跑一轮」），
                # 开着的时候你完全可以切到命中页点「存捡漏价」（那是个单字段写），
                # 或者在另一台机器的面板上改这条规则。整行写回会拿旧快照把
                # deal_price 冲回原值 —— 两次操作都提示成功，而后一次没了，
                # 人只会觉得「改了不生效」。（这个竞态原先是靠拉黑卖家踩出来的，
                # 拉黑现在走独立表、不再经 update_rule，但竞态本身还活着。）
                before = {k: clean(v) for k, v in rule.items() if k in store.RULE_FIELDS}
                payload = {k: v for k, v in payload.items() if v != before.get(k)}
                if not payload:
                    close()
                    notify("没有任何改动")
                    return
                store.update_rule(rule["id"], payload)
                # 【立刻重判、重算捡漏】和拉黑卖家、存捡漏价一个路子：都是纯 CPU、
                # 不发请求。不重判的话命中页会刷出「新摘要 + 旧徽标」——摘要按新规则算，
                # 每一行的捡漏/命中还是旧的，要等下一轮扫描才对得上。
                fresh = store.get_rule(rule["id"])
                poller.revalidate(fresh)
                poller.mark_deals(fresh)
            else:
                store.insert_rule(payload)
            close()
            rules_view.refresh()
            hits_view.refresh()
            notify("已保存。库里的商品已按新规则重判；抓取从下一轮起按新规则来"
                   if rule else "已保存，下一轮开始抓")

        def close() -> None:
            # 关了就删掉：每点一次「编辑」都会新建一个 dialog 元素，
            # 只 close 不 delete 的话它们会一直挂在页面上越积越多。
            dlg.close()
            dlg.delete()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=close).props(BTN_QUIET)
            ui.button("保存", on_click=save).props(BTN_PRIMARY)
    dlg.open()


def confirm_delete(rule: dict, host=None) -> None:
    with (host or ui.context.client.content):
        dlg = ui.dialog()
    with dlg, ui.card():
        ui.label(f"删除规则「{rule['name']}」？")
        ui.label("它抓到的商品和成交样本会一并删除，不可恢复。").classes("text-sm text-red-400")

        def close() -> None:
            dlg.close()
            dlg.delete()

        def do() -> None:
            store.delete_rule(rule["id"])
            close()
            rules_view.refresh()
            hits_view.refresh()
            notify("已删除")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=close).props(BTN_QUIET)
            ui.button("删除", on_click=do).props(BTN_DANGER_SOLID)
    dlg.open()


def _load_rules() -> dict:
    """规则页要的数据，4 条 SQL 取完（原先 33 条：每条规则 2 条 + 每个源 2 条）。"""
    return {"rules": store.get_rules(), "states": store.rule_states(),
            "sstates": store.source_states(), "counts": store.item_counts()}


# 装饰器挂在画界面的这个函数上，理由见 sold_view 那里。规则页的「刷新」不在建页签时
# 读 .refresh，所以它不像成交页那样当场空白 —— 它的表现是【保存/删除规则之后页面不更新】：
# 四处 rules_view.refresh() 全部抛 AttributeError 被 NiceGUI 吞进日志，
# 你看到的是"点了保存什么都没发生"。
@ui.refreshable
def rules_view(host=None) -> None:
    # 「新建规则」已经移到页面顶上的 toolbar 里，这里只列规则卡片
    d = _load_rules()
    for rule in d["rules"]:
        rid = rule["id"]
        st = d["states"].get(rid) or store.STATE_DEFAULT
        # 【规则级的数要对该规则【全部】源求和，不是只加当前启用的那几个】历史上抓过、
        # 后来从规则里摘掉的源，它的商品还在库里，「入库」这个数得把它们算进去 ——
        # 只加显示出来的源，把某条规则的源改窄之后这个数会悄悄变小，没人会发现。
        # 【命中＝当前在售的命中】和「命中」页显示的是同一批。
        # 用 SUM(matched) 会把已售出/已结束的历史命中也算进来，同一个词两个意思。
        total = sum(c["t"] for (r, _), c in d["counts"].items() if r == rid)
        hit = sum(c["h"] for (r, _), c in d["counts"].items() if r == rid)
        with ui.card().classes(CARD):
            with ui.row().classes("items-center w-full gap-3"):
                ui.label(rule["name"]).classes("text-base font-bold")
                ui.badge("启用" if rule["enabled"] else "停用",
                         color="green" if rule["enabled"] else "grey")
                ui.label(f'搜「{rule["keyword"]}」').classes("text-sm")
                ui.label(f"{yen(rule['price_min'])}〜{yen(rule['price_max'])}").classes("text-sm")
                ui.space()
                ui.label(f"入库 {total} · 在售命中 {hit}").classes("text-sm text-gray-400")
            ui.label(f"市价中位 {yen(st['median_price'])}"
                     f"（跨源 {st['sample_count']} 件成交）").classes("text-xs text-gray-300")

            # 每个数据源单独一行：各源独立计时、独立限速，状态也分开看
            for src in sources.for_rule(rule):
                ss = d["sstates"].get((rid, src.key)) or store.SOURCE_STATE_DEFAULT
                n = d["counts"].get((rid, src.key)) or {"t": 0, "h": 0}
                with ui.row().classes("gap-3 text-xs text-gray-400 items-center"):
                    ui.badge(src.name).classes(BADGE_LABEL)
                    ui.label(f"上次扫描 {ss['last_scan_at']:%m-%d %H:%M}"
                             if ss["last_scan_at"] else "还没扫过")
                    ui.label(f"在售 {ss['last_total']}")
                    ui.label(f"入库 {n['t']} · 在售命中 {n['h']}")
                    if ss["truncated"]:
                        ui.label("⚠ 上次没扫全，售出对账已跳过").classes("text-orange-400")
                    if ss["last_error"]:
                        ui.label(f"⚠ {ss['last_error'][:60]}").classes("text-red-400")
            if rule["note"]:
                # 封行宽，理由同设置页的说明：一行拉到 150 多字就没法读了
                ui.label(rule["note"]).classes(
                    "text-xs text-gray-400 max-w-4xl leading-relaxed")
            with ui.row().classes("gap-2"):
                ui.button("编辑", on_click=lambda r=rule: rule_dialog(r, host)).props(BTN_GHOST)
                ui.button("立即跑一轮", on_click=lambda r=rule: run_now(r)).props(BTN_GHOST)
                ui.button("删除", on_click=lambda r=rule: confirm_delete(r, host)
                          ).props(BTN_DANGER)

    # 【不要塞进 _load_rules】那边的测试是「发了哪几条 SQL」的精确相等，
    # 而这一块的查询是渲染时才发的，和规则数据不是一回事。
    _blocked_block()

# 【进程级的重入闸】面板是多标签页的，而抓取是服务端动作 ——
# 两个标签页各点一次，就是两轮并发打同一批源。各源的 _lock 会把请求串起来，
# 所以不会绕过节流，但配额会白烧一倍、日志也会交错到看不懂。
_fetching = {"busy": False}


async def fetch_all() -> None:
    """一键抓取：所有启用的规则立刻各跑一轮（常驻轮询照常继续，两边共用同一套限速）。"""
    if _fetching["busy"]:
        notify("已经在抓了，等这一轮跑完再点", type="warning")
        return
    rules = store.get_rules(enabled_only=True)
    if not rules:
        notify("没有启用的规则", type="warning")
        return
    # 【client 要在第一个 await 之前拿】await 回来时发起这个动作的按钮可能已经被
    # 别的 refresh 删掉了，那时 ui.notify 找不到容器会抛；用 client 当上下文就不依赖
    # 那个按钮。await 之后再拿就已经晚了（那一刻 slot 已经死了）。
    client = ui.context.client
    _fetching["busy"] = True
    try:
        st = store.get_settings()
        notify(f"开始抓 {len(rules)} 条规则 × {len(sources.all_sources())} 个源，"
               f"每个请求间隔 {st['req_delay_min']:g}〜{st['req_delay_max']:g} 秒，要几分钟…")
        new = matched = 0
        for r in rules:
            try:
                # 【每条规则单独 try】一条失败不该让后面几条一起放弃
                # 【走 manual】和后台轮询抢同一把扫描闸，拿不到就抛 ScanBusy
                stat = await run.io_bound(poller.manual, poller.run_once, store.get_rule(r["id"]))
                new += stat["new"]
                matched += stat["matched"]
            except poller.ScanBusy as e:
                with client:
                    notify(str(e), type="warning")
                break
            except Exception as e:          # noqa: BLE001 - 手动抓取失败只弹提示
                with client:
                    notify(f"「{r['name']}」失败：{e}", type="negative")
        with client:
            notify(f"抓完：新增 {new} 件，当前命中 {matched} 件", type="positive")
    finally:
        # 【必须在 finally】中途抛异常而不解锁的话，这个按钮就永久点不动了，
        # 而且没有任何办法恢复，只能重启进程。
        _fetching["busy"] = False
    hits_view.refresh()
    rules_view.refresh()
    all_view.refresh()


async def test_notify() -> None:
    """照着现在存着的推送设置，真发一条出去。

    【为什么非有这个按钮不可】推送失败是【静默】的：core.notify.post 出错只写一行
    日志，面板上什么都不显示（这是有意的 —— 推送坏了不该拖垮抓取）。代价是地址
    填错、模板写错、Slack webhook 被撤销，这些你全都看不出来，只会觉得"最近没捡漏"。
    而真捡漏一天就 0〜4 件，等它来验证等于没验证。
    这里把那个异常抓出来当场显示，是这条链路上唯一能立刻证伪的地方。
    """
    s = store.get_settings(force=True)          # 绕开 10 秒缓存，刚保存就能测
    url = (s.get("notify_url") or "").strip()
    if not url:
        notify("推送地址是空的 —— 填上「推送地址」再点（留空＝整个推送关闭，一个请求都不发）",
               type="warning")
        return
    # 用一件真商品的形状，这样你能看出到手的排版对不对，而不是只看到"test"
    text = ("🟢 捡漏 | 这是一条测试推送\n"
            "ASUS ROG ASTRAL GeForce RTX 5090 BTF\n"
            "¥850,000（市价的 106%，中位 ¥798,000）\n"
            "https://jp.mercari.com/item/m54103659696")
    client = ui.context.client          # 理由见 fetch_all
    try:
        # 【必须带一张真图】模板里有 {thumb} 时，不给图会走兜底的纯文本分支，
        # 那就测不出"带图的那条到底长什么样"—— 而那正是你点这个按钮想看的。
        thumb = (store.query("SELECT thumb_url FROM item WHERE thumb_url <> '' "
                             "ORDER BY last_seen_at DESC LIMIT 1") or [{}])[0].get("thumb_url", "")
        await run.io_bound(push.post, url, s.get("notify_body") or "", text, thumb)
    except Exception as e:      # noqa: BLE001 - 这里就是要把失败摆到脸上
        with client:
            notify(f"发送失败：{e}", type="negative")
        return
    with client:
        notify("已发出。去 Slack/手机看一眼 —— 没收到就是地址或请求体模板不对", type="positive")


async def pull_tracked() -> None:
    """追踪页的一键拉取：追踪中的每件立刻单独拉一次详情，不等 track_min 到点。

    【和「一键抓取」共用同一个闸，是故意的】两边打的是同一批源、烧的是同一份
    每日配额。各自一个闸的话，两个按钮同时点就是两拨请求并发出去 ——
    源自己的 _lock 会把它们串起来，所以不会超速，但你会对着一个几分钟不动的
    页面完全不知道在等什么。
    """
    if _fetching["busy"]:
        notify("已经在抓了，等这一轮跑完再点", type="warning")
        return
    # 【分母只算还在刷新的】卖掉/下架的留在追踪页上但不再发请求（tracked_due 只挑
    # on_sale/trading），拿总件数当分母的话，提示会说「另外 3 件没拉到」——那 3 件本来就不拉。
    rows = store.tracked_items()
    n = sum(1 for r in rows if r["status"] in store.LIVE)
    if not rows:
        notify("还没有追踪任何商品", type="warning")
        return
    if not n:
        notify(f"追踪中的 {len(rows)} 件都已结束，不再刷新", type="warning")
        return
    client = ui.context.client          # 理由见 fetch_all
    _fetching["busy"] = True
    try:
        st = store.get_settings()
        notify(f"开始拉 {n} 件的详情，每个请求间隔 "
               f"{st['req_delay_min']:g}〜{st['req_delay_max']:g} 秒…")
        done = await run.io_bound(poller.manual, poller.refresh_tracked, True)
        msg = f"拉完 {done} 件"
        if done < n:
            # 【差额要说出来】卖掉的会被自动摘掉追踪，读不出详情的会被跳过，
            # 两种都让件数对不上。不提的话，人只会觉得这按钮没干完活。
            msg += f"（另外 {n - done} 件没拉到，原因看日志）"
        with client:
            notify(msg, type="positive" if done else "warning")
    except poller.ScanBusy as e:
        with client:
            notify(str(e), type="warning")
    except Exception as e:          # noqa: BLE001 - 手动拉取失败只弹提示
        with client:
            notify(f"失败：{e}", type="negative")
    finally:
        # 理由同 fetch_all：不解锁的话这按钮就永久点不动了
        _fetching["busy"] = False
    track_view.refresh()
    # 拉到卖掉的会被自动摘掉追踪，那件商品会从这一页消失、出现在成交页
    sold_view.refresh()
    hits_view.refresh()


async def run_now(rule: dict) -> None:
    """面板上的手动试跑。走 io_bound 扔到线程里 —— 一轮要遍历所有源、发好几个请求、
    每个之间还要等 3〜8 秒，直接在事件循环里跑会把整个面板卡死。"""
    # 【和「一键抓取」共用同一个闸】原先只有它没有闸：连点两下就是两个 run_once
    # 并行，和后台轮询三方一起打同一批源。实测日志里 7 例同一规则×源几十秒内被扫两遍。
    if _fetching["busy"]:
        notify("已经在抓了，等这一轮跑完再点", type="warning")
        return
    client = ui.context.client          # 理由见 fetch_all
    srcs = "、".join(x.name for x in sources.for_rule(rule))
    st = store.get_settings()          # 别硬编码，这两个值在设置页可改
    notify(f"开始跑「{rule['name']}」（{srcs}），"
           f"每个请求间隔 {st['req_delay_min']:g}〜{st['req_delay_max']:g} 秒，请稍候…")
    _fetching["busy"] = True
    try:
        stat = await run.io_bound(poller.manual, poller.run_once, store.get_rule(rule["id"]))
        with client:
            notify(f"完成：在售{stat['total']}件 新增{stat['new']} "
                   f"降价{stat['price_down']} 命中{stat['matched']}", type="positive")
    except poller.ScanBusy as e:
        with client:
            notify(str(e), type="warning")
    except Exception as e:  # noqa: BLE001 - 手动试跑失败只该弹个提示，不该影响面板
        with client:
            notify(f"失败：{e}", type="negative")
    finally:
        _fetching["busy"] = False
    rules_view.refresh()
    hits_view.refresh()


# ------------------------------------------------------------------ 设置页

def _setting_note(it: dict) -> None:
    """说明栏：英文键 + 默认值一行，正文说明一行。

    【英文键必须留着】日志、报错、规则页备注里引用的都是它（"先把
    daily_request_limit 调大"），只显示中文名的话对不上号。
    【默认值并进同一行】它原先独占一行，16 项就白占 16 行，而它只在
    "我是不是改坏了"的时候才有人看。
    """
    d = repr(it["default"]) if it["type"] == "str" else it["default"]
    ui.label(f"{it['k']} · 默认 {d}").classes("text-xs text-gray-500")
    # 【必须封行宽】不封的话一行拉到 150 多字，眼睛从行尾回到下一行行首要重新找位置。
    # 说明里有换行（推送那几项列了各家的地址格式），预留换行才看得清。
    # break-words：说明里有 "(1440 / quick_min)" 这种断不开的串，
    # 手机宽度下会把整行顶出视口右边
    ui.label(it["note"]).classes(
        "text-xs text-gray-400 whitespace-pre-line leading-relaxed max-w-4xl break-words")


def _setting_row(it: dict, label: str, fields: dict) -> None:
    """设置页的一行。

    【左右分栏，不是上下排】原先一项一张卡、输入框和说明各占一行，16 项拉出三屏多，
    而右边 80% 是空的。分栏之后整页短了一多半，同组的几个值能一眼看全 ——
    这几个值恰恰是要互相参照着调的（间隔下限对上限、每日上限对每轮页数）。

    【字符串项例外】notify_url 是一串地址、notify_body 是 JSON 模板，
    塞进左边那个窄栏没法编辑，所以这两项仍旧上下排、输入框拉满。
    """
    with ui.element("div").classes("w-full border-t py-2"):
        if it["type"] == "str":
            if it["k"] == "notify_url":
                # 【这是一把钥匙】谁拿到都能往你手机推东西。设置页整串明文摆着，
                # 截图、共享屏幕、路过的人都看得到。遮住，要看再点眼睛。
                comp = ui.input(label, value=it["v"], password=True,
                                password_toggle_button=True) \
                    .classes("w-full max-w-3xl").props(INPUT + ' autocomplete="off"')
            else:
                comp = ui.input(label, value=it["v"]).classes("w-full max-w-3xl").props(INPUT)
            _setting_note(it)
        else:
            with ui.row().classes("items-start w-full gap-4 sm:flex-nowrap"):
                with ui.column().classes("gap-0 shrink-0 w-64"):
                    comp = ui.number(label, value=it["v"],
                                     format="%d" if it["type"] == "int" else "%.1f") \
                        .classes("w-full").props(INPUT)
                with ui.column().classes("gap-1 grow min-w-0 pt-1"):
                    _setting_note(it)
    fields[it["k"]] = (comp, it)


@ui.refreshable
def settings_view() -> None:
    fields: dict = {}

    # 这几项填 0 不是「关闭」而是各种翻车：
    #   sold_scan_hours=0  成交轮每 30 秒重跑一次
    #   max_pages=0        一件都扫不到，truncated 却判 False，于是售出对账
    #                      把全库在售当成失踪逐个花详情核实
    #   detail_budget=0    永远不核实、不读描述
    #   median_window_days=0  prune 会把整张 sold_sample 删空，tracked 样本再也抓不回来
    #   fresh_hours=0      两个「新」徽标永远不出现
    MUST_BE_POSITIVE = ("sold_scan_hours", "max_pages", "detail_budget",
                        "median_window_days", "fresh_hours", "daily_request_limit",
                        "missing_grace_min", "req_delay_min", "req_delay_max")

    def save() -> None:
        bad, bad_zero = [], []
        for k, (comp, it) in fields.items():
            val = comp.value
            # 【字符串项的空值是合法的】notify_url 留空就是"关掉推送"，
            # 走下面那条 bad 分支会变成"跳过不写"，于是根本关不掉。
            if it["type"] == "str":
                store.save_setting(k, val or "")
                continue
            if val is not None and k in MUST_BE_POSITIVE:
                try:
                    if float(val) <= 0:
                        bad_zero.append(k)
                        continue
                except (TypeError, ValueError):
                    pass
            if val is None:
                bad.append(k)                  # 空值会让该项回落默认，多半是误删，拦一下
                continue
            store.save_setting(k, val)
        settings_view.refresh()
        if bad_zero:
            notify(f"{'、'.join(bad_zero)} 不能填 0 或负数（这几项的 0 不是「关闭」，"
                   f"而是会让轮询退化或把成交样本删空），已跳过未保存", type="negative")
        if bad:
            # 【原来写的是「会按默认值走」，和事实正好相反】代码是 continue 跳过不写，
            # 库里的旧值继续生效。照着错提示操作的人会以为自己成功恢复了默认值。
            notify(f"已保存。{'、'.join(bad)} 留空了——【没有改动】，库里原来的值继续生效；"
                   f"要改请填具体数字", type="warning")
        else:
            notify("已保存，10 秒内生效")

    # 【保存按钮必须跟着滚】这一页十几项、好几屏长，按钮原先只在最底下：
    # 改完最上面那个「请求间隔下限」要一路滚到底才点得到，中途很容易忘了保存就切页，
    # 而这一页没有任何"有未保存改动"的提示 —— 改了等于没改，还看不出来。
    # 【top 不能是 0】ui.header() 是 Quasar 的 fixed-top，实测高 45px
    #（q-page-container 的 padding-top 就是它撑出来的）。top-0 的话这根条子
    # 一滚就钻到顶栏底下，按钮点不着 —— 比原先放在页面最底部还糟。
    # 【rw-toolbar 是补上来的】这一行是手写的 sticky 条、没走 toolbar()，
    # 漏了那个类的话「测试推送」是一句光秃秃的蓝字，而别的六页页首的「刷新」
    # 都是带底带框的按钮 —— 页面一多，这种参差比任何单点的丑都更显眼。
    with ui.row().classes("rw-toolbar sticky top-[45px] z-20 w-full items-center gap-3 "
                          "py-2 mb-2 backdrop-blur bg-[#1b1b1f]/95 rounded"):
        ui.button("保存全部", on_click=save).props(BTN_PRIMARY)
        ui.button("测试推送", on_click=test_notify).props(BTN_GHOST) \
            .tooltip("照现在【已保存】的推送设置真发一条出去。改完地址要先保存再测。"
                     "推送失败平时是静默的（只写日志），这是唯一能当场看出通没通的地方")
        # 【和 toolbar() 里那行同一个角色，就得同一个类】上面刚把按钮统一了，
        # 这行说明文字漏下的话，七页里只有设置页的说明小一号（12px vs 13px）。
        ui.label("对所有规则生效。改完保存，10 秒内自动生效，不用重启"
                 ).classes("rw-desc")

    # 【按 SETTING_GROUPS 渲染，不是按 all_settings 的顺序】没分组的项会整个看不见，
    # 所以 tests/test_settings_ui.py 锁死了两边的键必须完全一致。
    items = {it["k"]: it for it in store.all_settings()}
    for group, keys in config.SETTING_GROUPS.items():
        with ui.card().classes(CARD):
            ui.label(group).classes("text-sm font-medium text-gray-300")
            for k, label in keys:
                _setting_row(items[k], label, fields)


# ------------------------------------------------------------------ 组装

def create() -> None:
    @ui.page("/")
    def index() -> None:
        ui.dark_mode(True)
        # 【直接取参考项目的两个色值】app.css 的 --accent-dim #4a78c0（大面积填充
        # 那一档）和它的危险红 #f87171。原先用 oklch 是为了 P3 屏不被 sRGB 夹紧，
        # 但复刻的前提是色值对得上，而参考端给的就是这两个十六进制值。
        # 【主色只能从这里改】Quasar 的 .bg-primary 是 @layer quasar_importants 里的
        # !important，而 add_head_html 注入的 CSS 无层 —— 无层的 !important 打不过
        # 层里的 !important。所以硬碰是写不赢的，只能换 --q-primary 本身。
        # 【通知条的三种类型色也换成参考端的状态色】不换的话「已保存」「已经在抓了」
        # 这些提示是 Quasar 出厂的 #21BA45 / #F2C037，和徽标用的那套绿黄不是一家。
        # 深色字由 notify() 统一加，这三个亮底上都在 7:1 以上。
        ui.colors(primary="#4a78c0", negative="#f87171",
                  positive="#4ade80", warning="#facc15", info="#60a5fa")
        ui.add_head_html(DARK_CSS)
        # 【顶栏 = 参考端的 sticky header】半透明 96% + 背后模糊 8px + 1px 下边框。
        # 原先那句内联 .style() 必须去掉：内联样式赢过任何层、任何 !important，
        # 留着的话 DARK_CSS 里 .rw-header 那一段一条都不生效。
        # 【wrap=False 不是样式偏好，是 45px 的前提】默认 wrap=True 时状态那行字
        # 在窄屏会折行，顶栏从 45px 涨到 98px，设置页那根 sticky 保存条就被埋了。
        # 不折行 + CSS 里给状态字加省略号，窄屏上截断的是最不重要的尾部（时间戳），
        # 开头的 ⏸ / ⚠ 和那句话照常看得见。
        with ui.header(wrap=False).classes("items-center gap-2 px-3 py-2 rw-header"):
            # 【☰ 只在窄屏出现】lt-md 是 Quasar 自带的断点类（<1024px 才显示），
            # 和下面 left_drawer 的 breakpoint=1023 精确对齐：侧栏自动收起的
            # 那一刻，呼出它的按钮正好出现；宽屏上侧栏常驻，这颗按钮不存在。
            ui.button(icon="menu", on_click=lambda: nav.toggle()) \
                .props(BTN_CORNER).classes("lt-md")
            # 【页名在顶栏、导航在左栏】这是参考项目的分工（app.css L23 .title）。
            # 宽屏上左栏亮着的那一项已经说明你在哪页，这行是给窄屏用的 ——
            # 那时侧栏整个收起来，不写页名就不知道自己在看什么。
            page_title = ui.label("命中").classes("rw-title")
            ui.space()
            status = ui.label().classes("text-sm")

        # 【左侧导航栏】复刻参考端 app.css L39 的 .nav：210px、surface 底、
        # 右侧一条 1px 线，品牌在顶上，七个页签竖排成导航项。
        # 【top_corner=True 是关键】它把 Quasar 的 layout view 第 0 位从 h 改成 l，
        # 于是三件事全自动：侧栏从 y=0 起（参考端 .nav{top:0}）、顶栏自己吐出
        # left:210px、主区自己吐出 padding-left:210px。自己去写 margin-left 的话
        # 会和 Quasar 那份内联 padding 叠成两倍，主区被推到屏幕外。
        # 【宽度只能走 prop，不能走 CSS】Quasar 拿 props.width 同时算这三处；
        # 用 CSS 改宽度只改得动侧栏自己，另外两处还停在默认的 300px。
        # 【breakpoint=1023】≥1024px 常驻、没有任何收起入口；窄屏自动变成覆盖式
        # 抽屉，带遮罩和滑动关闭 —— 参考端手写的那套 .layout-mobile 在这儿是白送的。
        nav = ui.left_drawer(top_corner=True, bordered=False, elevated=False) \
            .props("width=210 breakpoint=1023").classes("nav-drawer")

        def tick() -> None:
            # 【先看采集还活着没】轮询线程崩掉后进程照常在跑、面板照常打开、
            # 旧数据照常显示，只有「上次扫描」的时间戳冻住 —— 不主动报的话
            # 你可能几天都以为它在监控。
            beat = poller.heartbeat()
            now = config.now()
            stalled = poller.stalled_for(now, beat, TOPBAR_STALL_MIN) if beat else None
            quota_out = False
            try:
                # 【一条 SQL】today_by_source 已经把每个源的 requests/errors 都取回来了，
                # 合计在 Python 里加就是；原先还多发一条 today_stat 算同一个数。
                rows = store.today_by_source()
                req = sum(x["requests"] for x in rows)
                err = sum(x["errors"] for x in rows)
                limit = store.get_settings()["daily_request_limit"]
                quota_out = req >= limit
                # 【失败数贴在各自的源后面】总数「失败 107」看不出是谁在失败；
                # 贴在源后面一眼就知道是 メルカリ 在被挡，而这个数本来就查回来了，零成本。
                per = "  ".join(f"{source_name(x['source'])} {x['requests']}"
                                + (f"（失败 {x['errors']}）" if x["errors"] else "")
                                for x in rows)
                text = (f"今日请求 {req}/{limit}" + (f"（{per}）" if per else "")
                        + (f"　失败 {err}" if err and not per else "") + f"　{now:%H:%M:%S}")
            except Exception as e:          # noqa: BLE001 - 数据库抽风不该让状态栏整个消失
                text = f"⚠ 读数据库失败：{str(e)[:60]}"
            holder = poller.lease_holder()
            # 【配额分支必须排在"停摆"前面】配额用完后主循环一睡 10 分钟，心跳看起来
            # 也像停了；先判配额，人才不会照着「重启进程」白折腾一趟。
            if quota_out:
                status.text = ("⏸ 今日请求已到上限，抓取暂停 —— 0 点（JST）自动恢复，"
                               "不用重启　" + text)
                status.classes(replace="text-sm text-amber-400 font-bold")
            elif beat is None or stalled is not None:
                gap = (f"已停 {fmt_minutes(stalled)}（心跳停在 {beat:%m-%d %H:%M}）"
                       if beat else "从未启动")
                status.text = (f"⚠ 轮询{gap} —— 进程还活着但没在抓，重启进程　" + text)
                status.classes(replace="text-sm text-red-400 font-bold")
            elif holder and holder != poller.ME:
                # 【待命要写出来】不写的话你看着本机的面板，以为它在抓 ——
                # 其实抓的是 NAS 那份，本机只是在看同一个库。
                status.text = f"待命：轮询由 {holder} 执行　" + text
                status.classes(replace="text-sm text-amber-400")
            else:
                status.text = text
                status.classes(replace="text-sm")

        tick()
        ui.timer(10.0, tick)

        # 对话框的家：建在所有 refreshable 容器之外，refresh() 清不到它
        dialog_host = ui.element()

        # 【切过去的时候才重建过时的那一页】配合 stale_tabs：点一面旗不再
        # 当场重建四个视图（1.24 秒 SQL），只把别的页记成过时；
        # 等你真切过去，那一下的重建藏在切页动作里，看不出来。
        # 【页签懒建】原先七个页签在打开页面那一刻全部同步建好：≈76 条 SQL、
        # 首屏 3〜7 秒，而你看的只是命中页。现在只建当前这一页，切过去时再建；
        # 建过的页改用 _DIRTY 决定要不要重建。
        # 【built 必须在 index() 里】每个浏览器标签一份。放模块级的话第二次打开
        # 面板时七个名字都已经"建过"，新页面的容器永远不填 —— 而第一次开是好的。
        containers: dict = {}
        built: set = set()

        def build_hits():
            with toolbar("当前符合条件的在售商品，按「市价的百分之多少」从低到高排"):
                ui.button("一键抓取", on_click=fetch_all).props(BTN_PRIMARY) \
                    .tooltip("所有启用的规则立刻各跑一轮。常驻轮询照常继续，"
                             "两边共用同一套限速，不会因此发得更快")
                ui.button("刷新", on_click=hits_view.refresh).props(BTN_QUIET) \
                    .tooltip("只重画页面，不发请求")
            hits_view(dialog_host)

        def build_track():
            with toolbar("盯住的几件。它们被单独拉详情刷新，价格、出价数、"
                         "是否卖掉都比整轮扫描快得多"):
                ui.button("一键拉取", on_click=pull_tracked).props(BTN_PRIMARY) \
                    .tooltip("追踪中的每件立刻各拉一次详情，不等刷新间隔到点。"
                             "请求之间要隔几秒，件数多就得等一会")
                ui.button("刷新", on_click=track_view.refresh).props(BTN_QUIET) \
                    .tooltip("只重画页面，不发请求")
            track_view()

        def build_marks():
            with toolbar("自己标下来的东西，纯记录。不发请求、不参与判定，"
                         "存的是你标记那一刻的快照，商品下架了也还在"):
                ui.button("刷新", on_click=marks_view.refresh).props(BTN_QUIET) \
                    .tooltip("只重画页面，不发请求")
            marks_view()

        def build_sold():
            with toolbar("市场实际用什么价清掉了什么货 —— 定价前先看分布，"
                         "别只看中位数那一个数字"):
                ui.button("刷新", on_click=sold_view.refresh).props(BTN_QUIET)
            sold_view()

        def build_all():
            rules = store.get_rules()
            opts = {None: "全部规则", **{r["id"]: r["name"] for r in rules}}
            with toolbar("抓到的每一件，含被判掉的。「具体原因」那一列说清是哪个词、"
                         "哪个阈值判的"):
                sel_rule = ui.select(opts, value=None).props(INPUT)
                sel_reason = ui.select(["全部"] + list(REASON_LABEL.values()),
                                       value="全部").props(INPUT)
                inp = ui.input(placeholder="搜标题 / 卖家ID").props(INPUT + " clearable debounce=400") \
                    .classes("w-56")
                redo = lambda: all_view.refresh(sel_rule.value, sel_reason.value, inp.value or "")  # noqa: E731
                ui.button("刷新", on_click=redo).props(BTN_QUIET)
            sel_rule.on_value_change(redo)
            sel_reason.on_value_change(redo)
            inp.on_value_change(redo)
            all_view(None, "全部")

        def build_rules():
            with toolbar("关键词、词表、价格区间、数据源都在这里。改完下一轮生效"):
                ui.button("新建规则",
                          on_click=lambda: rule_dialog(None, dialog_host)).props(BTN_PRIMARY)
            rules_view(dialog_host)

        def build_settings():
            # 【这一页不用 toolbar】保存按钮和那句说明都在 settings_view 顶上的
            # sticky 条里 —— 放这儿的话它不跟着滚，等于白放。
            settings_view()

        BUILD = {"命中": build_hits, "追踪": build_track, "标记": build_marks, "成交": build_sold,
                 "全部": build_all, "规则": build_rules, "设置": build_settings}

        def on_tab(e) -> None:
            # 【e.value 可能是 Tab 对象也可能是页签名】NiceGUI 两种都发得出来。
            name = e.value if isinstance(e.value, str) else TAB_NAME.get(e.value, "")
            # 【顶栏页名跟着页签走】必须放在下面容器判空【之前】：tab_panels
            # 构造时那次空触发也该把标题定成首页，否则窄屏首屏顶栏是空的。
            if name:
                page_title.text = name
            # 【tab_panels 构造时会先触发一次】那时容器一个都还没建，naive 版本会把
            # 命中页画到页面根上。拿不到容器就 return，首页在下面显式建。
            c = containers.get(name)
            if c is None:
                return
            if name not in built:
                built.add(name)
                _DIRTY.discard(name)
                with c:
                    BUILD[name]()
            elif name in _DIRTY:
                _DIRTY.discard(name)
                REFRESH[name].refresh()

        # 【七个页签原样搬进左栏，只是竖过来】QTabs 有原生 vertical，tabs 与
        # tab_panels 的联动是 Python 侧的 bind_value，不靠 DOM 相邻 —— 所以
        # 侧栏里的 tabs 和主区里的 tab_panels 照常同步，下面那整套懒建机制
        # （containers / built / _DIRTY / on_tab）一行都不用动。
        with nav:
            ui.label("Resale Watcher").classes("nav-brand")
            with ui.tabs(on_change=on_tab).props("vertical no-caps") \
                    .classes("w-full nav-tabs") as tabs:
                t_hit = ui.tab("命中")
                t_track = ui.tab("追踪")
                t_mark = ui.tab("标记")
                t_sold = ui.tab("成交")
                t_all = ui.tab("全部")
                t_rule = ui.tab("规则")
                t_set = ui.tab("设置")
        TAB_NAME = {t_hit: "命中", t_track: "追踪", t_mark: "标记", t_sold: "成交",
                    t_all: "全部", t_rule: "规则", t_set: "设置"}
        REFRESH = {"命中": hits_view, "追踪": track_view,
                   "标记": marks_view, "成交": sold_view}
        # animated=False 的理由见 DARK_CSS 里那条 overflow:visible 的注释：
        # 两者是一组，少一个要么 sticky 不生效、要么切页穿帮。
        with ui.tab_panels(tabs, value=t_hit, animated=False).classes("w-full"):
            for t, name in TAB_NAME.items():
                with ui.tab_panel(t) as panel:
                    containers[name] = panel
        # 首页当场建：上面 tab_panels 构造时那次 on_change 因为容器还没建已经 return 了
        built.add("命中")
        with containers["命中"]:
            build_hits()
