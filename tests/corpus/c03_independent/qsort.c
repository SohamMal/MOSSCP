#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int cmp_int(const void *x, const void *y) {
    const int *p = x;
    const int *q = y;
    return (*p > *q) - (*p < *q);
}

typedef struct {
    int *items;
    size_t len;
    size_t cap;
} Vec;

static void vec_init(Vec *v) {
    v->cap = 8;
    v->len = 0;
    v->items = malloc(v->cap * sizeof(int));
}

static void vec_push(Vec *v, int value) {
    if (v->len == v->cap) {
        v->cap *= 2;
        v->items = realloc(v->items, v->cap * sizeof(int));
    }
    v->items[v->len++] = value;
}

int main(void) {
    Vec v;
    vec_init(&v);
    int x;
    while (scanf("%d", &x) == 1) {
        vec_push(&v, x);
    }
    if (v.len == 0) {
        fputs("empty\n", stdout);
        free(v.items);
        return 1;
    }
    qsort(v.items, v.len, sizeof(int), cmp_int);
    long long acc = 0;
    for (size_t i = 0; i < v.len; i++) {
        acc += v.items[i];
        printf("%d ", v.items[i]);
    }
    printf("\nsum %lld\n", acc);
    free(v.items);
    return 0;
}
