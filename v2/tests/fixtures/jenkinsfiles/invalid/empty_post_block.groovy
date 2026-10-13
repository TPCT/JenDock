// expect: line 5: post condition 'always' has no steps
pipeline {
    agent any
    stages { stage('A') { steps { echo 'x' } } }
    post { always { } }
}
