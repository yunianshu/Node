import React, { useEffect, useState } from 'react'
import {
  Card, Progress, Typography, Space, Tag, Row, Col,
  Button, Skeleton, Toast,
} from '@douyinfe/semi-ui'
import { IconRefresh } from '@douyinfe/semi-icons'
import { novelApi } from '../api/novelApi'

const { Title, Text } = Typography

const API_NAMES = {
  'image-01': '图片生成',
  'MiniMax-Hailuo-2.3': '视频生成',
  'speech-2.8-hd': '语音合成',
  'music-2.6': '音乐生成',
  'MiniMax-M2.7-highspeed': '文本模型',
}

function Quota() {
  const [quota, setQuota] = useState(null)
  const [loading, setLoading] = useState(true)

  const fetchQuota = async () => {
    setLoading(true)
    try {
      const data = await novelApi.getQuota()
      setQuota(data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchQuota()
  }, [])

  const getStatusColor = (percent) => {
    if (percent >= 90) return 'red'
    if (percent >= 70) return 'orange'
    return 'green'
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title heading={3}>配额监控</Title>
        <Button icon={<IconRefresh />} onClick={fetchQuota} loading={loading}>刷新</Button>
      </div>

      {loading || !quota ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : (
        <Row gutter={[16, 16]}>
          {Object.entries(quota).map(([key, val]) => {
            const percent = Math.round((val.used / val.total) * 100)
            const color = getStatusColor(percent)
            return (
              <Col span={8} key={key}>
                <Card
                  title={
                    <Space>
                      <Text strong>{API_NAMES[key] || key}</Text>
                      <Tag color={color}>{percent}%</Tag>
                    </Space>
                  }
                >
                  <Space vertical align="start" style={{ width: '100%' }}>
                    <div style={{ width: '100%' }}>
                      <Progress percent={percent} stroke={color} showInfo={false} />
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between', width: '100%' }}>
                      <Text type="secondary" size="small">已用: {val.used.toLocaleString()} {val.unit}</Text>
                      <Text type="secondary" size="small">总额: {val.total.toLocaleString()} {val.unit}</Text>
                    </div>
                    <Text type="secondary" size="small">剩余: {(val.total - val.used).toLocaleString()} {val.unit}</Text>
                  </Space>
                </Card>
              </Col>
            )
          })}
        </Row>
      )}

      <Card style={{ marginTop: 24 }}>
        <Title heading={5}>配额策略说明</Title>
        <Space vertical align="start">
          <Text>1. 图片生成 (image-01): 200张/天，2000章需10天完成</Text>
          <Text>2. 视频生成 (Hailuo-2.3): 3个/天，建议仅生成预告片</Text>
          <Text>3. 语音合成 (speech-hd): 19000字符/天，配额充裕</Text>
          <Text>4. 音乐生成 (music-2.6): 100首/天，建议仅生成主题曲</Text>
          <Text>5. 文本模型: 20000 tokens/天，Coordinator 自动限流保护</Text>
        </Space>
      </Card>
    </div>
  )
}

export default Quota
