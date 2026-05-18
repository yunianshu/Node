import React, { useState } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { Layout as SemiLayout, Nav } from '@douyinfe/semi-ui'
import {
  IconBookOpenStroked,
  IconArticle,
  IconSetting,
  IconPieChartStroked,
  IconImage,
  IconFile,
} from '@douyinfe/semi-icons'

const { Sider, Content } = SemiLayout

const navItems = [
  { itemKey: '/projects', text: '小说项目', icon: <IconBookOpenStroked /> },
  { itemKey: '/chapters', text: '章节管理', icon: <IconArticle /> },
  { itemKey: '/config', text: '配置管理', icon: <IconSetting /> },
  { itemKey: '/quota', text: '配额监控', icon: <IconPieChartStroked /> },
  { itemKey: '/multimedia', text: '多媒体', icon: <IconImage /> },
  { itemKey: '/logs', text: '日志查看', icon: <IconFile /> },
]

function Layout({ children }) {
  const navigate = useNavigate()
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(false)

  const selectedKeys = [location.pathname === '/' ? '/projects' : location.pathname]

  return (
    <SemiLayout style={{ height: '100vh' }}>
      <Sider
        breakpoint={['md']}
        onBreakpoint={(screen, matched) => {
          if (matched) setCollapsed(true)
        }}
      >
        <Nav
          style={{ maxWidth: 220, height: '100%' }}
          items={navItems}
          selectedKeys={selectedKeys}
          onSelect={(data) => navigate(data.itemKey)}
          footer={{
            collapseButton: true,
            collapseText: collapsed ? '展开' : '收起',
          }}
          onCollapseChange={(isCollapsed) => setCollapsed(isCollapsed)}
        />
      </Sider>
      <Content
        style={{
          padding: 24,
          backgroundColor: 'var(--semi-color-bg-0)',
          overflow: 'auto',
        }}
      >
        {children}
      </Content>
    </SemiLayout>
  )
}

export default Layout
