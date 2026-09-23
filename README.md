# 记轴本

明日方舟 PC 端的帧级记轴小工具，窗口标题是 PTFE Toolkits。

在网页里记下「第几帧干什么」，到点提醒。也可以按轴回放部署、技能和撤退。帧数从费用条尺子读，尺子是另一个项目：[Arknights Cost Bar Ruler](https://github.com/ZeroAd-06/ArknightsCostBarRuler)。

动作是这份仓库里的 Python 自己做的。非官方工具，和鹰角网络没关系。辅助操作有账号风险，用不用自己决定。

## 跑起来

Windows，Python 3.12。

```bat
pip install -r requirements.txt
记轴本.bat
```

浏览器打开 http://127.0.0.1:2607 。给游戏窗口发按键需要管理员权限，bat 会自己弹授权。

旁边如果有 `python312\python.exe` 就用它，没有就用系统里的 `python`。第一次运行会在 `calib\` 里生成本机配置，那个目录不进仓库。

## 关卡坐标

格子和摄像机参数不在这个仓库里。要做屏幕坐标和格子的换算，把关卡 json 放到 `resource/map/`。数据来自 [Arknights-Tile-Pos](https://github.com/yuanyan3060/Arknights-Tile-Pos)（MIT，用的话请保留它的仓库地址）。没有这些文件时，记轴和到点提醒照常能用。

## 许可

[MIT](LICENSE)。
