/**
 * 小说生成系统 API 层
 * 对接 novels/ 框架数据，提供管理后台所需数据
 */

const PROJECTS = [
  {
    id: 'novels1',
    name: 'novels1',
    title: '原始项目',
    dir: 'D:/AiProject/Node/projects/novels1',
    totalChapters: 2000,
    completedChapters: 2000,
    avgScore: null,
    writerFailures: 0,
    jsonFailures: 0,
    status: 'completed',
    hasCover: false,
    hasTrailer: false,
    createdAt: '2025-04',
  },
  {
    id: 'novels2',
    name: 'novels2',
    title: '剑来风格',
    dir: 'D:/AiProject/Node/projects/novels2',
    totalChapters: 2000,
    completedChapters: 2000,
    avgScore: 8.19,
    writerFailures: 253,
    jsonFailures: 43,
    status: 'completed',
    hasCover: false,
    hasTrailer: false,
    createdAt: '2025-04',
  },
  {
    id: 'novels3',
    name: 'novels3',
    title: '一介书生，但武道通神',
    dir: 'D:/AiProject/Node/projects/novels3',
    totalChapters: 2000,
    completedChapters: 1997,
    avgScore: 7.77,
    writerFailures: 3,
    jsonFailures: 71,
    status: 'completed',
    hasCover: false,
    hasTrailer: false,
    createdAt: '2025-04',
  },
  {
    id: 'novels4',
    name: 'novels4',
    title: '版本 3 备份',
    dir: 'D:/AiProject/Node/projects/novels4',
    totalChapters: 2000,
    completedChapters: 122,
    avgScore: null,
    writerFailures: 0,
    jsonFailures: 0,
    status: 'completed',
    hasCover: false,
    hasTrailer: false,
    createdAt: '2025-05',
  },
  {
    id: 'novels5',
    name: 'novels5',
    title: '（独立脚本完成）',
    dir: 'D:/AiProject/Node/projects/novels5',
    totalChapters: 2000,
    completedChapters: 2000,
    avgScore: null,
    writerFailures: 0,
    jsonFailures: 0,
    status: 'completed',
    hasCover: false,
    hasTrailer: false,
    createdAt: '2025-05',
  },
  {
    id: 'novels6',
    name: 'novels6',
    title: '新书示例',
    dir: 'D:/AiProject/Node/projects/novels6',
    totalChapters: 2000,
    completedChapters: 0,
    avgScore: null,
    writerFailures: 0,
    jsonFailures: 0,
    status: 'pending',
    hasCover: true,
    hasTrailer: true,
    createdAt: '2025-05',
  },
  {
    id: 'novels7',
    name: 'novels7',
    title: '凡尘逆仙',
    dir: 'D:/AiProject/Node/projects/novels7',
    totalChapters: 200,
    completedChapters: 0,
    avgScore: null,
    writerFailures: 0,
    jsonFailures: 0,
    status: 'pending',
    hasCover: false,
    hasTrailer: false,
    createdAt: '2025-05',
  },
]

const DEFAULT_CONFIG = {
  title: '新书名称',
  project_dir: 'D:/AiProject/Node/projects/novels6',
  total_chapters: 2000,
  model: 'MiniMax-M2.7-highspeed',
  mmx_path: 'C:/Users/Administrator/AppData/Roaming/npm/node_modules/mmx-cli/dist/mmx.mjs',
  api_qps: 2.0,
  writer: {
    max_tokens: 8192,
    temperature: 0.7,
    context_chapters: 3,
    max_retries: 3,
    retry_delay: 5.0,
  },
  reviewer: {
    max_tokens: 4096,
    temperature: 0.3,
    min_score: 7.0,
    rewrite_threshold: 6.5,
    dimensions: ['文笔', '剧情', '人设', '连贯性'],
  },
  planner: {
    parallel_agents: 60,
    segments: 60,
    max_tokens: 8192,
    temperature: 0.7,
  },
  coordinator: {
    batch_size: 20,
    num_workers: 5,
    wechat_webhook: '',
    push_interval_seconds: 120,
    auto_fill_missing: true,
    auto_rewrite: true,
    pause_between_batches: 3.0,
  },
  multimedia: {
    enabled: true,
    images: {
      enabled: true,
      model: 'image-01',
      aspect_ratio: '16:9',
      generate_cover: true,
      generate_chapter_images: true,
    },
    videos: {
      enabled: false,
      model: 'MiniMax-Hailuo-2.3',
      generate_trailer: true,
      generate_chapter_videos: false,
      trailer_chapters: [1, 500, 1000, 1500, 2000],
      duration: '6s',
      resolution: '768p',
    },
    audio: {
      enabled: false,
      model: 'speech-2.8-hd',
      voice: 'Chinese_Mandarin_Gentle_Youth',
      generate_chapter_audio: true,
      speed: 1.0,
    },
    music: {
      enabled: false,
      model: 'music-2.6',
      generate_chapter_music: false,
      generate_theme_music: true,
      genre: 'dark folk',
      mood: 'mysterious, tense',
      vocals: 'male, deep, emotional',
      instruments: 'acoustic guitar, cello, piano',
      tempo: 'slow',
    },
  },
}

const QUOTA_MOCK = {
  'image-01': { used: 45, total: 200, unit: '张/天' },
  'MiniMax-Hailuo-2.3': { used: 1, total: 3, unit: '个/天' },
  'speech-2.8-hd': { used: 120, total: 19000, unit: '字符/天' },
  'music-2.6': { used: 5, total: 100, unit: '首/天' },
  'MiniMax-M2.7-highspeed': { used: 4500, total: 20000, unit: 'tokens/天' },
}

const MULTIMEDIA_STATUS = {
  novels1: { cover: false, trailer: false, images: 0, videos: 0, audio: 0, music: 0 },
  novels2: { cover: false, trailer: false, images: 0, videos: 0, audio: 0, music: 62 },
  novels3: { cover: false, trailer: false, images: 0, videos: 0, audio: 0, music: 0 },
  novels4: { cover: false, trailer: false, images: 0, videos: 0, audio: 0, music: 0 },
  novels5: { cover: false, trailer: false, images: 0, videos: 0, audio: 0, music: 0 },
  novels6: { cover: true, trailer: true, images: 1, videos: 1, audio: 0, music: 0 },
  novels7: { cover: false, trailer: false, images: 0, videos: 0, audio: 0, music: 0 },
}

// 模拟 API 调用延迟
const delay = (ms = 300) => new Promise((resolve) => setTimeout(resolve, ms))

export const novelApi = {
  async getProjects() {
    await delay(200)
    return PROJECTS
  },

  async getProjectDetail(projectId) {
    await delay(200)
    return PROJECTS.find((p) => p.id === projectId)
  },

  async getChapters(projectId, page = 1, pageSize = 20) {
    await delay(300)
    // 模拟章节数据
    const chapters = []
    const project = PROJECTS.find((p) => p.id === projectId)
    const total = project ? project.totalChapters : 2000
    const start = (page - 1) * pageSize + 1
    const end = Math.min(start + pageSize - 1, total)

    for (let i = start; i <= end; i++) {
      const hasDraft = i <= (project?.completedChapters || 0)
      const hasReview = hasDraft && project?.avgScore
      chapters.push({
        id: i,
        title: `第${i}章`,
        hasDraft,
        hasFinal: hasDraft && project?.status === 'completed',
        hasReview,
        score: hasReview ? (Math.random() * 3 + 6).toFixed(2) : null,
        wordCount: hasDraft ? Math.floor(Math.random() * 2000 + 3000) : 0,
        status: hasDraft ? (project?.status === 'completed' ? 'final' : 'draft') : 'pending',
      })
    }
    return { chapters, total }
  },

  async getConfig(projectId) {
    await delay(200)
    return { ...DEFAULT_CONFIG, title: `${projectId} 配置` }
  },

  async saveConfig(projectId, config) {
    await delay(500)
    console.log('保存配置:', projectId, config)
    return { success: true }
  },

  async getQuota() {
    await delay(300)
    return QUOTA_MOCK
  },

  async getMultimediaStatus(projectId) {
    await delay(200)
    return MULTIMEDIA_STATUS[projectId] || {}
  },

  async generateMedia(projectId, type, params) {
    await delay(1000)
    console.log('生成多媒体:', projectId, type, params)
    return { success: true, jobId: `job_${Date.now()}` }
  },

  async getLogs(projectId, logType = 'coordinator') {
    await delay(200)
    return [
      `[2025-05-18 10:00:00] [INFO] ${projectId} coordinator 启动`,
      `[2025-05-18 10:00:05] [INFO] 加载配置完成`,
      `[2025-05-18 10:00:10] [INFO] 开始生成批次 1-20`,
      `[2025-05-18 10:05:30] [INFO] 批次 1-20 完成`,
      `[2025-05-18 10:05:35] [INFO] 开始生成批次 21-40`,
      `[2025-05-18 10:10:20] [WARN] 遇到 2062 限流，等待 5s`,
      `[2025-05-18 10:10:26] [INFO] 限流恢复，继续生成`,
    ]
  },
}

export default novelApi
