#include <stdio.h>
#include <stdlib.h>

#define MAXN 1024

int read_array(int *a, int limit) {
    int n = 0;
    while (n < limit && scanf("%d", &a[n]) == 1) {
        n++;
    }
    return n;
}

void bubble_sort(int *a, int n) {
    for (int i = 0; i < n - 1; i++) {
        for (int j = 0; j < n - 1 - i; j++) {
            if (a[j] > a[j + 1]) {
                int tmp = a[j];
                a[j] = a[j + 1];
                a[j + 1] = tmp;
            }
        }
    }
}

int sum_array(int *a, int n) {
    int s = 0;
    for (int i = 0; i < n; i++) {
        s += a[i];
    }
    return s;
}

void dispatch(int choice, int *a, int n) {
    switch (choice) {
        case 1:
            bubble_sort(a, n);
            printf("sorted\n");
            break;
        case 2:
            printf("sum %d\n", sum_array(a, n));
            break;
        case 3:
            printf("count %d\n", n);
            break;
        default:
            printf("unknown\n");
    }
}

int main(void) {
    int a[MAXN];
    int n = read_array(a, MAXN);
    if (n == 0) {
        printf("empty\n");
        return 1;
    }
    dispatch(1, a, n);
    for (int i = 0; i < n; i++) {
        printf("%d ", a[i]);
    }
    printf("\n");
    return 0;
}
