import React, { useEffect, useState } from 'react'
import {
  Card, Typography, Space, Button, Tag, Table, Select, Toast,
  Skeleton, Row, Col, Progress,
} from '@douyinfe/semi-ui'
import {
  IconImage, IconVideo, IconMusic, IconMicrophone,
  IconPlay, IconRefresh,
} from '@douyinfe/semi-icons'
import { novelApi } from '../api/novelApi'

const { Title, Text } = Typography

const PROJECT_OPTIONS = [
  { value: 'novels1', label: 'novels1' },
  { value: 'novels2', label: 'novels2' },
  { value: 'novels3', label: 'novels3' },
  { value: 'novels4', label: 'novels4' },
  { value: 'novels5', label: 'novels5' },
  { value: 'novels6', label: 'novels6' },
  { value: 'novels7', label: 'novels7' },
]

const MEDIA_TYPES = [
  { key: 'cover', label: '封面', icon: <IconImage />, max: 1 },
  { key: 'trailer', label: '预告片', icon: <IconVideo />, max: 1 },
  { key: 'images', label: '章节配图', icon: <IconImage />, max: 2000 },
  { key: 'videos', label: '章节视频', icon: <IconVideo />, max: 5 },
  { key: 'audio', label: '语音朗读', icon: <IconMicrophone />, max: 2000 },
  { key: 'music', label: '背景音乐', icon: <IconMusic />, max: 2000 },
]

function Multimedia() {
  const [projectId, setProjectId] = useState('novels6')
  const [status, setStatus] = useState(null)
  const [loading, setLoading] = useState(true)

  const fetchStatus = async () => {
    setLoading(true)
    try {
      const data = await novelApi.getMultimediaStatus(projectId)
      setStatus(data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchStatus()
  }, [projectId])

  const handleGenerate = async (type) => {
    Toast.info(`正在生成 ${type}，请稍后...`)
    try {
      await novelApi.generateMedia(projectId, type)
      Toast.success(`${type} 生成任务已提交`)
      fetchStatus()
    } catch (e) {
      Toast.error('生成失败')
    }
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title heading={3}>多媒体管理</Title>
        <Space>
          <Select
            value={projectId}
            optionList={PROJECT_OPTIONS}
            onChange={setProjectId}
            style={{ width: 160 }}
          />
          <Button icon={<IconRefresh />} onClick={fetchStatus} loading={loading}>刷新</Button>
        </Space>
      </div>

      {loading || !status ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : (
        <>
          <Row gutter={[16, 16]}>
            {MEDIA_TYPES.map((media) => {
              const current = status[media.key] || 0
              const percent = media.max > 1 ? Math.round((current / media.max) * 100) : current >= 1 ? 100 : 0
              const isDone = media.max === 1 ? current >= 1 : current >= media.max

              return (
                <Col span={8} key={media.key}>
                  <Card
                    title={
                      <Space>
                        {media.icon}
                        <Text strong>{media.label}</Text>
                        {isDone ? <Tag color="green">已完成</Tag> : <Tag color="orange">进行中</Tag>}
                      </Space>
                    }
                  >
                    <Space vertical align="start" style={{ width: '100%' }}>
                      <div style={{ width: '100%' }}>
                        <Progress
                          percent={percent}
                          stroke={isDone ? 'green' : 'blue'}
                          showInfo
                          format={(p) => media.max === 1 ? (current >= 1 ? '有' : '无') : `${current}/${media.max}`}
                        />
                      </div>
                      <Button
                        icon={<IconPlay />}
                        type="primary"
                        size="small"
                        disabled={isDone}
                        onClick={() => handleGenerate(media.key)}
                      >
                        {media.max === 1 ? '生成' : '批量生成'}
                      </Button>
                    </Space>
                  </Card>
                </Col>
              )
            })}
          </Row>

          <Card style={{ marginTop: 24 }}>
            <Title heading={5}>生成策略建议</Title>
            <Space vertical align="start">
              <Text>1. 封面和预告片优先生成，每个项目仅1个</Text>
              <Text>2. 章节配图配额 200/天，全量生成需约10天</Text>
              <Text>3. 视频配额仅3/天，建议只生成关键章节（1, 500, 1000, 1500, 2000）</Text>
              <Text>4. 语音配额充裕（19000/天），可全量生成</Text>
              <Text>5. 音乐配额100/天，建议仅生成主题曲和关键章节背景音</Text>
            </Space>
          </Card>
        </>
      )}
    </div>
  )
}

export default Multimedia
