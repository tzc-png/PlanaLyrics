# PlanaLyrics

一个透明、置顶、无边框的桌面歌词覆盖层，跟随 MPRIS 播放器显示歌词。

为 Ubuntu 24.04 / X11 / GNOME 上自用而写，不追求通用适配——但凡是走 MPRIS 的播放器都能跟随，
包括浏览器里放的歌和 LX Music。

主要特点：

- **逐字进度**：当前句「唱到哪亮到哪」，而不是整句一起变色
- **多级歌词来源**：同名 `.lrc` → 音频内嵌标签 → 播放器自带 → 本地缓存 → 联网搜索
- **多站联网逐字时间轴**：酷狗 `krc`、QQ 音乐 `qrc`、网易云 `yrc`，逐个尝试
- **跟随壁纸明暗**：按面板背后的实际像素自动切换深色字 / 浅色字
- 右键菜单即全部设置，改配置文件即时生效，不用重启

## 环境要求

| | |
| --- | --- |
| 系统 | Ubuntu 24.04，**X11 会话**（不是 Wayland） |
| 桌面 | 任意有合成器的桌面（在 GNOME / Mutter 上开发） |
| Python | 3.12 或更高（用到 `tomllib`、`diff` 相关的新特性） |
| 依赖 | `python3-gi`、`gir1.2-gtk-3.0`、`python3-cairo`、`python3-mutagen`、`python3-requests`、`python3-numpy` |

Ubuntu 上装依赖：

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 python3-cairo python3-mutagen python3-requests python3-numpy
```

## 运行

```bash
python3 planalyrics.py
```

想让它出现在应用列表和坞栏里（并带上图标）：

```bash
./packaging/install.sh          # 安装启动器和图标
./packaging/install.sh remove   # 卸载
```

## 配置

配置在 `~/.config/PlanaLyrics/config.toml`，**改完保存即生效**，不需要重启。
仓库里的 `config.default.toml` 是出厂默认值：首次运行会复制过去，菜单里的「恢复默认设置」也用它覆盖。

几个常改的：

```toml
[player]
pin = ""              # 留空 = 跟随正在播放的来源；填播放器名字可固定

[window]
panel_width = 512     # 启动时的窗口大小（拖动改的只影响本次运行）
panel_height = 256
font_scale = 100      # 字号缩放百分比，菜单里的「增大/减小字号」改的就是它
karaoke = true        # 逐字进度
adapt_to_background = true
all_workspaces = true # 所有工作区都显示

[lyrics]
online_enabled = false    # 联网搜索歌词与译文
search_translation = true # 外语歌联网补一条中文译文
offset_ms = 0             # 整首歌词微调，负数 = 提前
```

窗口的位置和大小在拖动之后只影响本次运行；想固定下来，用右键菜单的
**窗口 → 保存窗口大小和位置**。

## 右键菜单

```
歌词    播放器 / 重新搜索歌词 / 显示翻译 / 联网搜索歌词 / 联网搜索中文译文
窗口    增大字号 / 减小字号 / 跨页面显示 / 保存窗口大小和位置
显示    逐字进度 / 使用awlrc / 使用联网逐字歌词 / 支持增强LRC / 跟随壁纸明暗
程序    打开配置文件夹 / 重载配置 / 退出
恢复默认设置
```

「显示」里带 **（当前）** 标记的那一条，是这首歌此刻实际在用哪种逐字时间轴。

窗口左下角有一个小圆点：**红色**表示还有查找没回来，**绿色**表示都完成了。

## 逐字时间轴从哪来

同一句话的逐字时间可以有好几个来源，按下面的顺序取第一个能用的：

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `awlrc` | 歌词自带的 AMLL / Lyricify 逐字轴 |
| 2 | 联网 | 酷狗 `krc` → QQ 音乐 `qrc` → 网易云 `yrc` |
| 3 | `enhanced` | 增强 LRC 里的行内 `<mm:ss.xx>` 标记 |

三条都可以在菜单里单独关掉，全部落空时回退到按句子平均速度扫描。

联网那一档有两条准入规则，都是为了让「有扫描」和「扫描是对的」一致：

- **必须真的落在字上**——站点给的文字跟这份歌词对不上就不算数，换下一个候选
- **必须覆盖过半的行**——站点对同一首歌常有好几个剪辑（短版、翻唱、加速版），
  拿一个只对得上前几句的版本，会让扫描走一段停一段

歌词本身带 `awlrc` 时**根本不联网**：它的优先级最高，取回来的也永远不会显示。

配置里出现「纯音乐」这类提示时同样不联网——本来就没有词可找。

## 联网会访问哪些站点

| 用途 | 站点 |
| --- | --- |
| 歌词、译文、`yrc` | `music.163.com` |
| `krc` | `krcs.kugou.com`、`lyrics.kugou.com` |
| `qrc` | `c.y.qq.com`、`u.y.qq.com` |

结果缓存在 `~/.cache/PlanaLyrics/` 下，所以同一首歌第二次播放是离线的。
关掉 `lyrics.online_enabled` 就不再联网找歌词（逐字时间轴另有开关 `window.karaoke_online`）。

## 已知限制

- **只支持 X11**。置顶和窗口穿透依赖 X11 的行为，Wayland 下不工作。
- 站点的时间轴若来自**另一个剪辑**（不同母带、remix、翻唱），逐字会有整体偏差。
  代码会把它压制在每句之内，但句与句之间的对不齐无法消除。
- 逐字扫描按 100 ms 重绘。**一行里塞进二十几个字**（比如 0.5 秒唱完一句）时，
  一帧就要跨过大半行——这是刷新率的上限，不是 bug。
- 只对 MPRIS 播放器工作；播放器不上报元数据时无法定位歌曲。

## 许可

MIT，见 [LICENSE](LICENSE)。

`planalyrics/qqdes.py` 是 QQ 音乐 QRC 加解密的移植，来自
[WXRIW/QQMusicDecoder](https://github.com/WXRIW/QQMusicDecoder)（MIT）。
第三方代码及其许可证原文见 [THIRD-PARTY.md](THIRD-PARTY.md)。
