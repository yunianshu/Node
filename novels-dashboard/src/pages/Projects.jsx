import React, { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Card, Typography, Tag, Button, Progress,
  Space, Row, Col, Skeleton, Toast,
} from '@douyinfe/semi-ui'
import { IconPlay, IconSetting, IconFile } from '@douyinfe/semi-icons'
import { novelApi } from '../api/novelApi'

const { Title, Text } = Typography

const statusMap = {
  completed: { color: 'green', text: '已完成' },
  running: { color: 'blue', text: '运行中' },
  pending: { color: 'orange', text: '待启动' },
  error: { color: 'red', text: '异常' },
}

function Projects() {
  const [projects, setProjects] = useState([])
  const [loading, setLoading] = useState(true)
  const navigate = useNavigate()

  useEffect(() => {
    novelApi.getProjects().then((data) => {
      setProjects(data)
      setLoading(false)
    })
  }, [])

  const handleStart = (projectId) => {
    Toast.success(`已启动 ${projectId} 生成任务`)
  }

  const totalCompleted = projects.reduce((s, p) => s + p.completedChapters, 0)
  const totalTarget = projects.reduce((s, p) => s + p.totalChapters, 0)

  return (
    <div>
      <Title heading={3} style={{ marginBottom: 24 }}>小说项目管理</Title>

      {/* 概览统计 */}
      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={6}>
          <Card>
            <div style={{ textAlign: 'center' }}>
              <Typography.Text type="secondary">项目总数</Typography.Text>
              <div style={{ fontSize: 32, fontWeight: 600, marginTop: 8 }}>{projects.length}</div>
            </div>
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <div style={{ textAlign: 'center' }}>
              <Typography.Text type="secondary">总章节数</Typography.Text>
              <div style={{ fontSize: 32, fontWeight: 600, marginTop: 8 }}>{totalCompleted}<span style={{ fontSize: 14, fontWeight: 400, color: 'var(--semi-color-text-2)' }}> / {totalTarget}</span></div>
            </div>
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <div style={{ textAlign: 'center' }}>
              <Typography.Text type="secondary">已完成项目</Typography.Text>
              <div style={{ fontSize: 32, fontWeight: 600, marginTop: 8 }}>{projects.filter((p) => p.status === 'completed').length}</div>
            </div>
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <div style={{ textAlign: 'center' }}>
              <Typography.Text type="secondary">平均评分</Typography.Text>
              <div style={{ fontSize: 32, fontWeight: 600, marginTop: 8 }}>{projects.filter((p) => p.avgScore).length > 0
                ? (projects.filter((p) => p.avgScore).reduce((s, p) => s + p.avgScore, 0) / projects.filter((p) => p.avgScore).length).toFixed(2)
                : '-'}</div>
            </div>
          </Card>
        </Col>
      </Row>

      {/* 项目卡片 */}
      <Row gutter={16}>
        {loading ? (
          [1, 2, 3, 4].map((i) => (
            <Col span={12} key={i} style={{ marginBottom: 16 }}>
              <Skeleton active paragraph={{ rows: 4 }} />
            </Col>
          ))
        ) : (
          projects.map((project) => {
            const status = statusMap[project.status] || statusMap.pending
            const progress = Math.round((project.completedChapters / project.totalChapters) * 100)

            return (
              <Col span={12} key={project.id} style={{ marginBottom: 16 }}>
                <Card
                  title={
                    <Space>
                      <Text strong>{project.title}</Text>
                      <Tag color={status.color}>{status.text}</Tag>
                    </Space>
                  }
                  style={{ height: '100%' }}
                >
                  <Space vertical align="start" style={{ width: '100%' }}>
                    <Text type="secondary">项目: {project.name}</Text>
                    <Text type="secondary">目录: {project.dir}</Text>

                    <div style={{ width: '100%', marginTop: 8 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                        <Text size="small">进度</Text>
                        <Text size="small">{project.completedChapters} / {project.totalChapters}</Text>
                      </div>
                      <Progress percent={progress} showInfo />
                    </div>

                    {project.avgScore && (
                      <Text size="small">平均评分: <Text strong>{project.avgScore}</Text></Text>
                    )}
                    {project.writerFailures > 0 && (
                      <Text size="small" type="warning">Writer 失败: {project.writerFailures} 次</Text>
                    )}
                    {project.jsonFailures > 0 && (
                      <Text size="small" type="warning">JSON 解析失败: {project.jsonFailures} 次</Text>
                    )}

                    <div style={{ marginTop: 8 }}>
                      {project.hasCover && <Tag size="small" style={{ marginRight: 4 }}>封面</Tag>}
                      {project.hasTrailer && <Tag size="small" style={{ marginRight: 4 }}>预告片</Tag>}
                    </div>

                    <Space style={{ marginTop: 12 }}>
                      <Button
                        icon={<IconPlay />}
                        type="primary"
                        disabled={project.status === 'completed'}
                        onClick={() => handleStart(project.id)}
                      >
                        启动生成
                      </Button>
                      <Button
                        icon={<IconSetting />}
                        onClick={() => navigate(`/config?project=${project.id}`)}
                      >
                        配置
                      </Button>
                      <Button
                        icon={<IconFile />}
                        onClick={() => navigate(`/chapters?project=${project.id}`)}
                      >
                        章节
                      </Button>
                    </Space>
                  </Space>
                </Card>
              </Col>
            )
          })
        )}
      </Row>
    </div>
  )
}

export default Projects
