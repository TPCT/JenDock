// expect: line 7: must not contain '..'
pipeline {
    agent any
    stages {
        stage('A') {
            steps {
                dockerBuild(image: 'app', dockerfile: '../Dockerfile')
            }
        }
    }
}
