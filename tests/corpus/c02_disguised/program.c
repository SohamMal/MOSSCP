#include <stdio.h>
#include <stdlib.h>

#define CAP 1024

/* add up every element */
int total_of(int *data, int count) {
    int acc = 0;
    int idx = 0;
    while (idx < count) {
        acc = acc + data[idx];
        idx = idx + 1;
    }
    return acc;
}

void order(int *data, int count) {
    int i = 0;
    while (i < count - 1) {
        int j = 0;
        while (j < count - 1 - i) {
            if (data[j + 1] < data[j]) {
                int hold = data[j];
                data[j] = data[j + 1];
                data[j + 1] = hold;
            }
            j = j + 1;
        }
        i = i + 1;
    }
}

int fill(int *data, int cap) {
    int count = 0;
    while (count < cap && scanf("%d", &data[count]) == 1) {
        count = count + 1;
    }
    return count;
}

void handle(int option, int *data, int count) {
    if (option == 1) {
        order(data, count);
        printf("sorted\n");
    } else if (option == 2) {
        printf("sum %d\n", total_of(data, count));
    } else if (option == 3) {
        printf("count %d\n", count);
    } else {
        printf("unknown\n");
    }
}

int main(void) {
    int data[CAP];
    int count = fill(data, CAP);
    if (count == 0) {
        printf("empty\n");
        return 1;
    }
    handle(1, data, count);
    int p = 0;
    while (p < count) {
        printf("%d ", data[p]);
        p = p + 1;
    }
    printf("\n");
    return 0;
}
