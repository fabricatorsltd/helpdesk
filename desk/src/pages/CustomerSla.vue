<template>
  <div class="flex flex-col overflow-y-auto">
    <LayoutHeader>
      <template #left-header>
        <Breadcrumbs :items="breadcrumbs" />
      </template>
    </LayoutHeader>
    <div
      class="flex flex-col gap-5 py-6 h-full flex-1 self-center overflow-auto mx-auto w-full max-w-4xl px-5"
    >
      <div
        v-if="policies.data?.policies?.length"
        class="flex flex-col gap-6"
      >
        <div
          v-for="pol in policies.data.policies"
          :key="pol.sla"
          class="flex flex-col gap-3 rounded border border-outline-gray-2 bg-surface-gray-1 p-5"
        >
          <div class="flex flex-wrap gap-2">
            <span
              v-for="lvl in pol.levels"
              :key="lvl.value"
              class="rounded bg-surface-gray-3 px-2 py-0.5 text-p-sm text-ink-gray-7"
            >
              {{ lvl.label }}
            </span>
          </div>
          <div class="prose-f text-sm" v-html="sanitize(pol.policy_html)" />
        </div>
      </div>
      <div v-else-if="policies.data" class="text-p-sm text-ink-gray-5">
        {{ __("No SLA policy is active for your account.") }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { LayoutHeader } from "@/components";
import { __ } from "@/translation";
import { Breadcrumbs, createResource, usePageMeta } from "frappe-ui";
import sanitizeHtml from "sanitize-html";
import { computed } from "vue";

const policies = createResource({
  url: "fab_helpdesk.api.get_customer_sla_policies",
  auto: true,
});

const breadcrumbs = computed(() => [
  { label: __("SLA"), route: { name: "CustomerSla" } },
]);

function sanitize(html: string) {
  return sanitizeHtml(html, {
    allowedTags: sanitizeHtml.defaults.allowedTags.concat(["img"]),
  });
}

usePageMeta(() => ({
  title: __("SLA"),
}));
</script>
