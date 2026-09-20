<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'; import { marked } from 'marked'
import type { Citation } from '../api'
const props=defineProps<{content:string;citations:Citation[]}>(),emit=defineEmits<{citation:[Citation]}>()
const html=computed(()=>DOMPurify.sanitize(marked.parse(props.content,{async:false}) as string,{ALLOWED_URI_REGEXP:/^(?:(?:https?):|[^a-z]|[a-z+.-]+(?:[^a-z+.-:]|$))/i}))
function click(e:MouseEvent){const text=(e.target as HTMLElement).textContent||'';const n=Number(text.match(/\[(\d+)\]/)?.[1]);const citation=props.citations.find(x=>x.number===n);if(citation){e.preventDefault();emit('citation',citation)}}
</script>
<template><div class="message-content" v-html="html" @click="click"/></template>
