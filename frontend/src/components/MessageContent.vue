<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'; import { marked } from 'marked'
import type { Citation } from '../api'
const props=defineProps<{content:string;citations:Citation[];activeCitation?:number}>(),emit=defineEmits<{citation:[Citation]}>()
const html=computed(()=>{
  const safe=DOMPurify.sanitize(marked.parse(props.content,{async:false}) as string,{ALLOWED_URI_REGEXP:/^(?:(?:https?):|[^a-z]|[a-z+.-]+(?:[^a-z+.-:]|$))/i})
  const numbers=new Set(props.citations.map(item=>item.number))
  return safe.replace(/\[(\d+)\]/g,(label,value)=>{const number=Number(value);if(!numbers.has(number))return label;const active=props.activeCitation===number?' active':'';return `<button type="button" class="citation-ref${active}" data-citation="${number}" aria-label="查看引用 ${number}">${label}</button>`})
})
function click(e:MouseEvent){const target=(e.target as HTMLElement).closest<HTMLElement>('[data-citation]');const n=Number(target?.dataset.citation);const citation=props.citations.find(x=>x.number===n);if(citation){e.preventDefault();emit('citation',citation)}}
</script>
<template><div class="message-content" v-html="html" @click="click"/></template>
