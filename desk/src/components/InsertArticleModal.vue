<template>
  <Dialog v-model="show" :options="{ title: __('Insert article') }">
    <template #body-content>
      <div class="flex flex-col gap-3">
        <TextInput
          v-model="query"
          type="text"
          :placeholder="__('Search the knowledge base')"
          :debounce="300"
          autofocus
        >
          <template #prefix>
            <LucideSearch class="size-4" />
          </template>
        </TextInput>
        <div
          v-if="results.length"
          class="flex max-h-80 flex-col gap-1 overflow-y-auto"
        >
          <button
            v-for="a in results"
            :key="a.name"
            class="flex flex-col items-start gap-0.5 rounded px-2 py-1.5 text-left hover:bg-surface-gray-2"
            @click="select(a)"
          >
            <span class="text-base text-ink-gray-9">{{ a.subject }}</span>
            <!-- eslint-disable-next-line vue/no-v-html -->
            <span
              class="line-clamp-1 text-p-sm text-ink-gray-5"
              v-html="a.description"
            ></span>
          </button>
        </div>
        <p
          v-else-if="query.length > 2 && !articles.loading"
          class="text-p-sm text-ink-gray-5"
        >
          {{ __("No answers found") }}
        </p>
      </div>
    </template>
  </Dialog>
</template>

<script setup lang="ts">
import { __ } from "@/translation";
import { createResource, Dialog, TextInput } from "frappe-ui";
import { computed, ref, watch } from "vue";

interface Article {
  name: string;
  subject: string;
  description: string;
}

const props = defineProps<{ modelValue: boolean }>();
const emit = defineEmits<{
  (event: "update:modelValue", value: boolean): void;
  (event: "select", article: { id: string; title: string }): void;
}>();

const show = computed({
  get: () => props.modelValue,
  set: (value) => emit("update:modelValue", value),
});
const query = ref("");
// agents search across every language: the customer's language is theirs to judge
const articles = createResource({ url: "helpdesk.api.article.search", auto: false });
// one hit per heading comes back: keep the first per article
const results = computed<Article[]>(() => {
  const seen = new Set<string>();
  return (articles.data || []).filter((a: Article) => {
    const id = a.name.split("#")[0];
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
});

watch(query, (value) => {
  if (value.length < 3) return;
  articles.update({ params: { query: value } });
  articles.reload();
});
watch(show, (value) => {
  if (!value) {
    query.value = "";
    articles.data = [];
  }
});

function select(article: Article) {
  emit("select", { id: article.name.split("#")[0], title: article.subject });
  show.value = false;
}
</script>
