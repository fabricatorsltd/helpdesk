<template>
  <Dialog v-model:open="show" :title="__('Merge')">
    <template #default>
      <div class="flex flex-col gap-4">
        <p class="text-p-base text-ink-gray-8">
          {{
            __(
              "All comments and emails of the other selected tickets will be merged into the target ticket."
            )
          }}
        </p>

        <div class="flex flex-col gap-1">
          <span class="text-xs text-ink-gray-5">{{ __("Target ticket") }}</span>
          <div class="flex max-h-64 flex-col overflow-y-auto">
            <label
              v-for="ticket in eligibleTickets"
              :key="ticket.name"
              class="flex cursor-pointer items-start gap-2 rounded-md p-2 hover:bg-surface-gray-2"
            >
              <input
                v-model="targetTicket"
                type="radio"
                class="mt-1"
                :value="ticket.name"
              />
              <span class="text-p-base text-ink-gray-8">
                <span class="font-semibold">#{{ ticket.name }}</span>
                <span class="ms-1">{{ ticket.subject }}</span>
              </span>
            </label>
          </div>
        </div>

        <div v-if="skippedCount" class="text-sm text-ink-gray-6">
          {{
            __(
              "{0} selected ticket(s) cannot be merged and will be ignored.",
              String(skippedCount)
            )
          }}
        </div>

        <!-- banner -->
        <div
          class="flex items-center gap-2 rounded-md p-2 ring-1 ring-outline-elevation-2"
        >
          <TriangleAlert
            class="h-6 w-5 w-min-5 w-max-5 min-h-5 max-w-5 text-ink-yellow-5"
          />

          <div class="text-wrap text-sm text-ink-gray-7">
            {{ __("This action is irreversible.") }}
          </div>
        </div>
      </div>
    </template>
    <template #actions>
      <Button
        class="w-full"
        variant="solid"
        :label="
          sourceTickets.length
            ? __(
                'Merge {0} tickets into #{1}',
                String(sourceTickets.length),
                String(targetTicket)
              )
            : __('Select at least two tickets')
        "
        :disabled="!sourceTickets.length"
        :loading="merging"
        :icon-left="LucideMerge"
        @click="handleMerge"
      />
    </template>
  </Dialog>
</template>

<script setup lang="ts">
import { __ } from "@/translation";
import { call, createListResource, Dialog, toast } from "frappe-ui";
import { computed, ref, watch } from "vue";
import LucideMerge from "~icons/lucide/merge";
import TriangleAlert from "~icons/lucide/triangle-alert";

interface Ticket {
  name: string;
  subject: string;
  status_category: string;
  is_merged: number;
}

interface E {
  (event: "success"): void;
}

const props = defineProps<{
  selections: Set<string>;
}>();
const emit = defineEmits<E>();
const show = defineModel<boolean>();

const targetTicket = ref<string | null>(null);
const merging = ref(false);

const tickets = createListResource({
  doctype: "HD Ticket",
  fields: ["name", "subject", "status_category", "is_merged"],
  pageLength: 999,
  onSuccess: () => {
    // oldest ticket, the lowest number, is the default target
    targetTicket.value = eligibleTickets.value[0]?.name ?? null;
  },
});

// same conditions as TicketMergeModal: a merged or resolved ticket is neither
// a valid source nor a valid target
const eligibleTickets = computed<Ticket[]>(() =>
  ((tickets.data as Ticket[]) ?? [])
    .filter(
      (ticket) =>
        !ticket.is_merged && ["Open", "Paused"].includes(ticket.status_category)
    )
    .sort((a, b) => Number(a.name) - Number(b.name))
);

const skippedCount = computed(
  () => props.selections.size - eligibleTickets.value.length
);

const sourceTickets = computed(() =>
  targetTicket.value
    ? eligibleTickets.value
        .filter((ticket) => ticket.name !== targetTicket.value)
        .map((ticket) => ticket.name)
    : []
);

watch(
  () => show.value,
  (opened) => {
    if (!opened) return;
    targetTicket.value = null;
    tickets.update({
      filters: { name: ["in", Array.from(props.selections)] },
    });
    tickets.reload();
  }
);

async function handleMerge() {
  merging.value = true;
  try {
    for (const source of sourceTickets.value) {
      await call("helpdesk.helpdesk.doctype.hd_ticket.api.merge_ticket", {
        source,
        target: targetTicket.value,
      });
    }
    toast.success(__("Tickets merged successfully."));
  } catch (error: any) {
    toast.error(error?.messages?.[0] || error.message);
  } finally {
    merging.value = false;
    show.value = false;
    // some tickets may already be merged, refresh either way
    emit("success");
  }
}
</script>

<style scoped></style>
