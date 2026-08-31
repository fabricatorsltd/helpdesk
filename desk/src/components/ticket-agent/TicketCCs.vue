<template>
  <div class="mt-4 flex flex-col gap-2">
    <span class="text-xs text-ink-gray-5">{{ __("CCs") }}</span>
    <div v-if="ccs.length" class="flex flex-wrap gap-1.5">
      <span
        v-for="email in ccs"
        :key="email"
        class="flex items-center gap-1 rounded bg-surface-gray-3 px-2 py-0.5 text-p-sm text-ink-gray-7"
      >
        {{ email }}
        <button
          class="text-ink-gray-5 hover:text-ink-gray-8"
          :title="__('Remove')"
          @click="remove(email)"
        >
          <LucideX class="h-3 w-3" />
        </button>
      </span>
    </div>
    <Autocomplete
      :placeholder="__('Add a contact...')"
      :options="candidates.data || []"
      :value="null"
      @change="onSelect"
    />
  </div>
</template>

<script setup lang="ts">
import { Autocomplete } from "@/components";
import { __ } from "@/translation";
import { TicketSymbol } from "@/types";
import { call, createResource } from "frappe-ui";
import { computed, inject } from "vue";
import LucideX from "~icons/lucide/x";

const ticket = inject(TicketSymbol)!;

const ccs = computed(() =>
  (ticket.value?.doc?.fab_cc || "")
    .split(",")
    .map((s: string) => s.trim().toLowerCase())
    .filter(Boolean)
);

const candidates = createResource({
  url: "fab_helpdesk.api.get_cc_candidates",
  makeParams: () => ({ ticket: ticket.value.name }),
  auto: true,
});

function run(method: string, email: string) {
  return call("run_doc_method", {
    dt: "HD Ticket",
    dn: ticket.value.name,
    method,
    args: { email },
  }).then(() => {
    ticket.value.reload();
    candidates.reload();
  });
}

function onSelect(option: { value?: string } | null) {
  if (option?.value) run("add_cc", option.value);
}

function remove(email: string) {
  run("remove_cc", email);
}
</script>
