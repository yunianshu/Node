import React, { useEffect, useState } from 'react'
import {
  Typography, Space, Select, Button, Card, List,
  Input, Skeleton, Toast,
} from '@douyinfe/semi-ui'
import { IconRefresh, IconSearch } from '@douyinfe/semi-icons'
import { novelApi } from '../api/novelApi'

const { Title, Text } = Typography

const PROJECT_OPTIONS = [
  { value: 'novels2', label: 'novels2' },
  { value: 'novels3', label: 'novels3' },
  { value: 'novels4', label: 'novels4' },
  { value: 'novels5', label: 'novels5' },
]

const LOG_TYPES = [
  { value: 'coordinator', label: 'Coordinator' },
  { value: 'writer', label: 'Writer' },
  { value: 'reviewer', label: 'Reviewer' },
  { value: 'multimedia', label: '多媒体' },
]

function Logs() {
  const [projectId, setProjectId] = useState('novels5')
  const [logType, setLogType] = useState('coordinator')
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [searchText, setSearchText] = useState('')

  const fetchLogs = async () => {
    setLoading(true)
    try {
      const data = await novelApi.getLogs(projectId, logType)
      setLogs(data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchLogs()
  }, [projectId, logType])

  const filteredLogs = logs.filter((line) =>
    line.toLowerCase().includes(searchText.toLowerCase())
  )

  const getLogLevelColor = (line) => {
    if (line.includes('[ERROR]')) return 'var(--semi-color-danger)'
    if (line.includes('[WARN]')) return 'var(--semi-color-warning)'
    if (line.includes('[INFO]')) return 'var(--semi-color-success)'
    return 'var(--semi-color-text-2)'
  }

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <Title heading={3}>日志查看</Title>
        <Space>
          <Select
            value={projectId}
            optionList={PROJECT_OPTIONS}
            onChange={setProjectId}
            style={{ width: 160 }}
          />
          <Select
            value={logType}
            optionList={LOG_TYPES}
            onChange={setLogType}
            style={{ width: 140 }}
          />
          <Input
            prefix={<IconSearch />}
            placeholder="搜索日志"
            value={searchText}
            onChange={setSearchText}
            style={{ width: 200 }}
          />
          <Button icon={<IconRefresh />} onClick={fetchLogs} loading={loading}>
            刷新
          </Button>
        </Space>
      </div>

      {loading ? (
        <Skeleton active paragraph={{ rows: 12 }} />
      ) : (
        <Card style={{ backgroundColor: 'var(--semi-color-bg-1)' }}>
          <div
            style={{
              fontFamily: 'monospace',
              fontSize: 13,
              lineHeight: '24px',
              maxHeight: '70vh',
              overflow: 'auto',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
            }}
          >
            {filteredLogs.length === 0 ? (
              <Text type="secondary">暂无日志</Text>
            ) : (
              filteredLogs.map((line, idx) => (
                <div key={idx} style={{ color: getLogLevelColor(line), padding: '2px 0' }}>
                  {line}
                </div>
              ))
            )}
          </div>
        </Card>
      )}
    </div>
  )
}

export default Logs
