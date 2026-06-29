# 太阳系动态运行网页

## Concept & Vision
一个沉浸式的3D太阳系可视化网页，展示太阳和八大行星围绕太阳运行的壮观场景。深邃的太空背景配合流畅的轨道动画，营造出宇宙的神秘与壮美。

## Design Language
- **Aesthetic**: 深空科幻风格，深蓝黑色背景点缀繁星
- **Color Palette**:
  - Background: #000011 (深空黑)
  - Sun: #FDB813 (太阳金黄)
  - Accent: #4A90D9 (星空蓝)
- **Typography**: Orbitron (科技感字体), sans-serif fallback
- **Motion**: 行星沿椭圆轨道恒速/变速运行，土星带旋转动画，星空闪烁

## Layout & Structure
- 全屏 Canvas 3D 场景
- 左上角标题 "Solar System"
- 底部图例显示行星名称和公转周期

## Features & Interactions
- 8大行星围绕太阳公转（按真实比例缩放的周期）
- 太阳位于中心，持续发光效果
- 各行星带有独特的纹理/颜色
- 土星有标志性环带
- 可鼠标拖拽旋转视角，滚轮缩放
- 星空背景层

## Component Inventory
- Sun: 发光球体，带光晕效果
- Planets: 8个球体，大小和颜色各异
- Rings: 土星环
- Orbit paths: 半透明轨道线
- Stars: 随机分布的背景星点

## Technical Approach
- Three.js 3D渲染
- OrbitControls 视角控制
- PointLight 模拟太阳光源
- 动画循环更新行星位置