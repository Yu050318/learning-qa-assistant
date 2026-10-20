import type {DocumentTask} from './api'

export const taskKinds={ingest:'文档入库',hierarchy:'层级补建',delete:'版本退休清理',delete_series:'整组资料退休'}
export const taskStates={queued:'等待处理',running:'处理中',succeeded:'已完成',failed:'未完成',cancelled:'已取消'}
const phases:Record<string,string>={queued:'等待处理',running:'开始处理',validating:'校验文件',uploading:'传输文档',parsing:'解析文档',splitting:'切分内容',embedding:'生成向量',indexing:'写入索引',verifying:'核验索引',cleaning_versions:'逐版清理',cleaning_index:'清理向量',cleaning_files:'清理源文件',waiting_runs:'等待关联运行结束',retry_wait:'等待自动重试',done:'已完成',failed:'处理失败',cancelled:'已取消',legacy:'历史任务'}
export function taskPhase(task:DocumentTask){return phases[task.phase]||taskStates[task.state]}
export function taskPercent(task:DocumentTask){
  if(task.state==='succeeded')return 100
  if(task.total_units===null||task.total_units<=0)return null
  return Math.min(99,Math.max(0,Math.floor(task.processed_units/task.total_units*100)))
}
export function taskHint(task:DocumentTask){
  if(task.kind==='delete_series'&&task.error_code==='DOCUMENT_SERIES_CLEANUP_INCOMPLETE')return '部分版本清理失败，请在关联的版本退休任务中检查并重试；已完成的版本不会重复清理。'
  const hints:Record<string,string>={DOCUMENT_INDEX_PINNED:'此版本仍被问答运行引用，相关运行结束后会自动继续清理。',INGESTION_ATTEMPTS_EXHAUSTED:'已达到自动重试上限，请检查服务后重试。',DOCUMENT_INDEX_UNAVAILABLE:'原索引连接不可用，恢复连接后再重试；不会清理其他位置。',INDEX_BUILD_CONFIGURATION_CHANGED:'构建参数已变化，请恢复原参数，或上传新的候选版本。',INDEX_VERIFICATION_FAILED:'索引核验未通过，当前可检索版本保持不变。',SOURCE_FILE_MISSING:'源文件已丢失，请重新上传。',SUPERSEDED_BY_RETIREMENT:'文档已提交退休，由新的清理任务继续处理。',SUPERSEDED_BY_RETRY:'已由新的任务继续处理。'}
  return task.error_code?(hints[task.error_code]||'后台未完成本次处理，请检查服务和任务详情。'):''
}
