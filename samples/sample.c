#include <stdio.h>
#include "sample.h"

#define MAX_COUNT 10
#define SQUARE(x) ((x) * (x))

typedef unsigned int uint32;

typedef struct Item {
    int id;
    const char *name;
} Item;

enum State {
    STATE_INIT,
    STATE_DONE
};

static void helper(int value)
{
    printf("value=%d\n", value);
}

int add(int a, int b)
{
    helper(a);
    return a + b;
}

int main(void)
{
    Item item = {1, "demo"};
    int result = add(item.id, SQUARE(2));
    printf("result=%d\n", result);
    return 0;
}
